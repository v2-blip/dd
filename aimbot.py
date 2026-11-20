import ctypes
import cv2
import json
import hashlib
import math
import mss
import os
import sys
import shutil
import time
import threading
import queue
import torch
import numpy as np
import win32api
from termcolor import colored
from ultralytics import YOLO
import socket
from ctypes import wintypes
import uuid
import struct


def _enable_per_monitor_dpi_awareness():
    """Keep MSS capture pixels and Win32 overlay coordinates in the same space."""
    try:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        setter = user32.SetProcessDpiAwarenessContext
        setter.argtypes = [ctypes.c_void_p]
        setter.restype = wintypes.BOOL
        if setter(ctypes.c_void_p(-4)):  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            return True
    except Exception:
        pass
    try:
        shcore = ctypes.WinDLL('shcore', use_last_error=True)
        setter = shcore.SetProcessDpiAwareness
        setter.argtypes = [ctypes.c_int]
        setter.restype = ctypes.c_long
        return setter(2) == 0  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        return False


_DPI_AWARENESS_ENABLED = _enable_per_monitor_dpi_awareness()


class _OverlayRECT(ctypes.Structure):
    _fields_ = [
        ('left', wintypes.LONG),
        ('top', wintypes.LONG),
        ('right', wintypes.LONG),
        ('bottom', wintypes.LONG),
    ]


class _OverlayPAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ('hdc', wintypes.HDC),
        ('fErase', wintypes.BOOL),
        ('rcPaint', _OverlayRECT),
        ('fRestore', wintypes.BOOL),
        ('fIncUpdate', wintypes.BOOL),
        ('rgbReserved', wintypes.BYTE * 32),
    ]


class TriggerStatusOverlay:
    """Small green/red, click-through status dot on the primary monitor.

    This is separate from the notification-area (system tray) icon. The tray
    launcher reads the state file; this class draws the original on-screen dot.
    """

    # Win32 constants
    WS_POPUP = 0x80000000
    WS_EX_TOPMOST = 0x00000008
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_LAYERED = 0x00080000
    WS_EX_NOACTIVATE = 0x08000000
    SW_SHOWNOACTIVATE = 4
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040
    HWND_TOPMOST = ctypes.c_void_p(-1)
    LWA_ALPHA = 0x00000002
    WM_PAINT = 0x000F
    WM_ERASEBKGND = 0x0014
    WM_NCHITTEST = 0x0084
    WM_MOUSEACTIVATE = 0x0021
    HTTRANSPARENT = -1
    MA_NOACTIVATE = 3
    PS_NULL = 5
    PM_REMOVE = 0x0001

    DIAMETER = 12
    MARGIN = 4

    def __init__(self):
        self.state_path = os.environ.get('LUNAR_TRIGGER_STATE_FILE')
        self.enabled = False
        self.hwnd = None
        self.dot_size = 12
        self.dot_corner = 'bottom_right'
        self.color_on = '#35D49A'
        self.color_off = '#E45D70'
        self.class_name = 'LunarTriggerDot_' + uuid.uuid4().hex
        self.user32 = ctypes.WinDLL('user32', use_last_error=True)
        self.gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
        self.kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self._configure_api()
        self._wndproc_ref = self._make_wndproc()
        self._create_dot()
        self.set_enabled(False)

    def _configure_api(self):
        LRESULT = ctypes.c_ssize_t
        WPARAM = ctypes.c_size_t
        LPARAM = ctypes.c_ssize_t
        self.WNDPROC = ctypes.WINFUNCTYPE(
            LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM
        )

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ('style', wintypes.UINT),
                ('lpfnWndProc', self.WNDPROC),
                ('cbClsExtra', ctypes.c_int),
                ('cbWndExtra', ctypes.c_int),
                ('hInstance', wintypes.HINSTANCE),
                ('hIcon', wintypes.HICON),
                ('hCursor', wintypes.HANDLE),
                ('hbrBackground', wintypes.HBRUSH),
                ('lpszMenuName', wintypes.LPCWSTR),
                ('lpszClassName', wintypes.LPCWSTR),
            ]
        self.WNDCLASSW = WNDCLASSW

        class MSG(ctypes.Structure):
            _fields_ = [
                ('hwnd', wintypes.HWND),
                ('message', wintypes.UINT),
                ('wParam', WPARAM),
                ('lParam', LPARAM),
                ('time', wintypes.DWORD),
                ('pt', wintypes.POINT),
            ]
        self.MSG = MSG

        u = self.user32
        g = self.gdi32
        u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        u.RegisterClassW.restype = wintypes.ATOM
        u.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        u.CreateWindowExW.restype = wintypes.HWND
        u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
        u.DefWindowProcW.restype = LRESULT
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.ShowWindow.restype = wintypes.BOOL
        u.UpdateWindow.argtypes = [wintypes.HWND]
        u.UpdateWindow.restype = wintypes.BOOL
        u.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        u.SetWindowPos.restype = wintypes.BOOL
        u.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
        u.SetWindowRgn.restype = ctypes.c_int
        u.SetLayeredWindowAttributes.argtypes = [
            wintypes.HWND, wintypes.DWORD, wintypes.BYTE, wintypes.DWORD,
        ]
        u.SetLayeredWindowAttributes.restype = wintypes.BOOL
        u.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(_OverlayPAINTSTRUCT)]
        u.BeginPaint.restype = wintypes.HDC
        u.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(_OverlayPAINTSTRUCT)]
        u.EndPaint.restype = wintypes.BOOL
        u.PeekMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        u.PeekMessageW.restype = wintypes.BOOL
        u.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
        u.TranslateMessage.restype = wintypes.BOOL
        u.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
        u.DispatchMessageW.restype = LRESULT
        u.DestroyWindow.argtypes = [wintypes.HWND]
        u.DestroyWindow.restype = wintypes.BOOL
        u.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        u.UnregisterClassW.restype = wintypes.BOOL
        u.InvalidateRect.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
        u.InvalidateRect.restype = wintypes.BOOL

        g.CreateEllipticRgn.argtypes = [ctypes.c_int] * 4
        g.CreateEllipticRgn.restype = wintypes.HANDLE
        g.CreateSolidBrush.argtypes = [wintypes.DWORD]
        g.CreateSolidBrush.restype = wintypes.HBRUSH
        g.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        g.CreatePen.restype = wintypes.HPEN
        g.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        g.SelectObject.restype = wintypes.HANDLE
        g.Ellipse.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        g.Ellipse.restype = wintypes.BOOL
        g.DeleteObject.argtypes = [wintypes.HANDLE]
        g.DeleteObject.restype = wintypes.BOOL

    @staticmethod
    def _hex_to_colorref(value):
        try:
            value = str(value).lstrip('#')
            if len(value) != 6:
                raise ValueError('invalid color')
            r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
            return r | (g << 8) | (b << 16)
        except (TypeError, ValueError):
            return 0x003AD495

    def _position(self):
        monitor_w = self.user32.GetSystemMetrics(0)
        monitor_h = self.user32.GetSystemMetrics(1)
        size = max(1, int(self.dot_size))
        margin = self.MARGIN
        positions = {
            'top_left': (margin, margin),
            'top_right': (max(0, monitor_w - size - margin), margin),
            'bottom_left': (margin, max(0, monitor_h - size - margin)),
            'bottom_right': (
                max(0, monitor_w - size - margin),
                max(0, monitor_h - size - margin),
            ),
        }
        return positions.get(self.dot_corner, positions['bottom_right'])

    def update_appearance(self, color_on=None, color_off=None, corner=None, size=None):
        if color_on:
            self.color_on = str(color_on)
        if color_off:
            self.color_off = str(color_off)
        if corner in ('top_left', 'top_right', 'bottom_left', 'bottom_right'):
            self.dot_corner = corner
        if size is not None:
            self.dot_size = max(0, min(100, int(size)))

        if not self.hwnd:
            return
        if self.dot_size <= 0:
            self.user32.ShowWindow(self.hwnd, 0)  # SW_HIDE
            return

        region = self.gdi32.CreateEllipticRgn(0, 0, self.dot_size, self.dot_size)
        if region:
            if not self.user32.SetWindowRgn(self.hwnd, region, True):
                self.gdi32.DeleteObject(region)
                print('[WARN] Could not resize status-dot region.', flush=True)
        x, y = self._position()
        self.user32.SetWindowPos(
            self.hwnd, self.HWND_TOPMOST, x, y, self.dot_size, self.dot_size,
            self.SWP_NOACTIVATE | self.SWP_SHOWWINDOW,
        )
        self.user32.InvalidateRect(self.hwnd, None, True)
        self.user32.UpdateWindow(self.hwnd)
        self.user32.ShowWindow(self.hwnd, self.SW_SHOWNOACTIVATE)

    def _make_wndproc(self):
        @self.WNDPROC
        def wndproc(hwnd, message, wparam, lparam):
            if message == self.WM_NCHITTEST:
                return self.HTTRANSPARENT
            if message == self.WM_MOUSEACTIVATE:
                return self.MA_NOACTIVATE
            if message == self.WM_ERASEBKGND:
                return 1
            if message == self.WM_PAINT:
                ps = _OverlayPAINTSTRUCT()
                hdc = self.user32.BeginPaint(hwnd, ctypes.byref(ps))
                color = self._hex_to_colorref(self.color_on if self.enabled else self.color_off)
                brush = self.gdi32.CreateSolidBrush(color)
                pen = self.gdi32.CreatePen(self.PS_NULL, 0, color)
                old_brush = self.gdi32.SelectObject(hdc, brush)
                old_pen = self.gdi32.SelectObject(hdc, pen)
                self.gdi32.Ellipse(hdc, 0, 0, self.dot_size, self.dot_size)
                self.gdi32.SelectObject(hdc, old_brush)
                self.gdi32.SelectObject(hdc, old_pen)
                self.gdi32.DeleteObject(brush)
                self.gdi32.DeleteObject(pen)
                self.user32.EndPaint(hwnd, ctypes.byref(ps))
                return 0
            return self.user32.DefWindowProcW(hwnd, message, wparam, lparam)
        return wndproc

    def _create_dot(self):
        # Make coordinates physical-pixel accurate on scaled/multi-monitor desktops.
        try:
            self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass

        hinstance = self.kernel32.GetModuleHandleW(None)
        wc = self.WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinstance
        wc.lpszClassName = self.class_name
        if not self.user32.RegisterClassW(ctypes.byref(wc)):
            error = ctypes.get_last_error()
            if error != 1410:  # class already exists
                raise ctypes.WinError(error)

        # Primary-monitor metrics are relative to the primary monitor's origin.
        monitor_w = self.user32.GetSystemMetrics(0)
        monitor_h = self.user32.GetSystemMetrics(1)
        x = max(0, monitor_w - self.DIAMETER - self.MARGIN)
        y = max(0, monitor_h - self.DIAMETER - self.MARGIN)
        ex_style = (
            self.WS_EX_TOPMOST | self.WS_EX_TRANSPARENT | self.WS_EX_TOOLWINDOW
            | self.WS_EX_LAYERED | self.WS_EX_NOACTIVATE
        )
        self.hwnd = self.user32.CreateWindowExW(
            ex_style, self.class_name, 'Lunar Trigger Status', self.WS_POPUP,
            x, y, self.DIAMETER, self.DIAMETER,
            None, None, hinstance, None,
        )
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

        region = self.gdi32.CreateEllipticRgn(0, 0, self.DIAMETER, self.DIAMETER)
        if not region:
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.user32.SetWindowRgn(self.hwnd, region, True):
            self.gdi32.DeleteObject(region)
            raise ctypes.WinError(ctypes.get_last_error())

        if not self.user32.SetLayeredWindowAttributes(self.hwnd, 0, 255, self.LWA_ALPHA):
            raise ctypes.WinError(ctypes.get_last_error())
        self.user32.SetWindowPos(
            self.hwnd, self.HWND_TOPMOST, x, y, self.DIAMETER, self.DIAMETER,
            self.SWP_NOACTIVATE | self.SWP_SHOWWINDOW,
        )
        self.user32.ShowWindow(self.hwnd, self.SW_SHOWNOACTIVATE)
        self.user32.UpdateWindow(self.hwnd)
        print('[INFO] On-screen status dot created on the primary monitor.')

    def set_enabled(self, enabled):
        enabled = bool(enabled)
        changed = enabled != self.enabled
        self.enabled = enabled
        if self.state_path:
            try:
                from pathlib import Path
                Path(self.state_path).write_text('1' if enabled else '0', encoding='ascii')
            except OSError as exc:
                print(f'[WARN] Could not update tray trigger state: {exc}')
        if self.hwnd and changed:
            self.user32.InvalidateRect(self.hwnd, None, True)
            self.user32.UpdateWindow(self.hwnd)
        if self.hwnd:
            self.update_appearance()

    def pump(self):
        if not self.hwnd:
            return
        msg = self.MSG()
        while self.user32.PeekMessageW(ctypes.byref(msg), self.hwnd, 0, 0, self.PM_REMOVE):
            self.user32.TranslateMessage(ctypes.byref(msg))
            self.user32.DispatchMessageW(ctypes.byref(msg))

    def close(self):
        self.set_enabled(False)
        if self.hwnd:
            self.user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        try:
            hinstance = self.kernel32.GetModuleHandleW(None)
            self.user32.UnregisterClassW(self.class_name, hinstance)
        except Exception:
            pass


class AimFOVOverlay:
    """Transparent, click-through circular FOV ring on the primary monitor."""

    WS_POPUP = 0x80000000
    WS_EX_TOPMOST = 0x00000008
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_LAYERED = 0x00080000
    WS_EX_NOACTIVATE = 0x08000000
    SW_HIDE = 0
    SW_SHOWNOACTIVATE = 4
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040
    HWND_TOPMOST = ctypes.c_void_p(-1)
    LWA_COLORKEY = 0x00000001
    WM_PAINT = 0x000F
    WM_ERASEBKGND = 0x0014
    WM_NCHITTEST = 0x0084
    WM_MOUSEACTIVATE = 0x0021
    HTTRANSPARENT = -1
    MA_NOACTIVATE = 3
    PS_SOLID = 0
    NULL_BRUSH = 5
    PM_REMOVE = 0x0001
    RING_COLOR = (65, 157, 255)
    PADDING = 5

    def __init__(self):
        self.user32 = ctypes.WinDLL('user32', use_last_error=True)
        self.gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
        self.kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self.class_name = 'LunarAimFOV_' + uuid.uuid4().hex
        self.hwnd = None
        self._class_brush = None
        self._hinstance = None
        self.diameter = 0
        self.window_size = 0
        self.visible = False
        self.ring_color = tuple(self.RING_COLOR)
        self.ring_thickness = 3
        self._last_geometry = None
        self._last_topmost_refresh = 0.0
        self._wndproc_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
            ctypes.c_size_t, ctypes.c_ssize_t
        )
        self._wndproc_ref = self._make_wndproc()
        self._register_and_create()

    def _register_and_create(self):
        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ('style', wintypes.UINT),
                ('lpfnWndProc', self._wndproc_type),
                ('cbClsExtra', ctypes.c_int),
                ('cbWndExtra', ctypes.c_int),
                ('hInstance', wintypes.HINSTANCE),
                ('hIcon', wintypes.HICON),
                ('hCursor', wintypes.HANDLE),
                ('hbrBackground', wintypes.HBRUSH),
                ('lpszMenuName', wintypes.LPCWSTR),
                ('lpszClassName', wintypes.LPCWSTR),
            ]
        self.WNDCLASSW = WNDCLASSW
        self.user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        self.user32.RegisterClassW.restype = wintypes.ATOM
        self.user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        self.user32.CreateWindowExW.restype = wintypes.HWND
        self.user32.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t
        ]
        self.user32.DefWindowProcW.restype = ctypes.c_ssize_t
        self.user32.SetLayeredWindowAttributes.argtypes = [
            wintypes.HWND, wintypes.DWORD, wintypes.BYTE, wintypes.DWORD
        ]
        self.user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
        self.user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT
        ]
        self.user32.SetWindowPos.restype = wintypes.BOOL
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.ShowWindow.restype = wintypes.BOOL
        self.user32.UpdateWindow.argtypes = [wintypes.HWND]
        self.user32.UpdateWindow.restype = wintypes.BOOL
        self.user32.InvalidateRect.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
        self.user32.InvalidateRect.restype = wintypes.BOOL
        self.user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(_OverlayPAINTSTRUCT)]
        self.user32.BeginPaint.restype = wintypes.HDC
        self.user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(_OverlayPAINTSTRUCT)]
        self.user32.EndPaint.restype = wintypes.BOOL
        self.user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(_OverlayRECT), wintypes.HBRUSH]
        self.user32.FillRect.restype = ctypes.c_int
        self.user32.PeekMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        self.user32.PeekMessageW.restype = wintypes.BOOL
        self.user32.TranslateMessage.argtypes = [ctypes.c_void_p]
        self.user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
        self.user32.DispatchMessageW.restype = ctypes.c_ssize_t
        self.user32.DestroyWindow.argtypes = [wintypes.HWND]
        self.user32.DestroyWindow.restype = wintypes.BOOL
        self.user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        self.user32.UnregisterClassW.restype = wintypes.BOOL

        self.gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        self.gdi32.CreatePen.restype = wintypes.HPEN
        self.gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        self.gdi32.SelectObject.restype = wintypes.HANDLE
        self.gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        self.gdi32.DeleteObject.restype = wintypes.BOOL
        self.gdi32.Ellipse.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        self.gdi32.Ellipse.restype = wintypes.BOOL
        self.gdi32.MoveToEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        self.gdi32.MoveToEx.restype = wintypes.BOOL
        self.gdi32.LineTo.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        self.gdi32.LineTo.restype = wintypes.BOOL
        self.gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
        self.gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
        self.gdi32.GetStockObject.argtypes = [ctypes.c_int]
        self.gdi32.GetStockObject.restype = wintypes.HANDLE

        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        hinstance = self.kernel32.GetModuleHandleW(None)
        self._hinstance = hinstance
        black_brush = self.gdi32.CreateSolidBrush(0)
        self._class_brush = black_brush
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinstance
        wc.hbrBackground = black_brush
        wc.lpszClassName = self.class_name
        if not self.user32.RegisterClassW(ctypes.byref(wc)):
            error = ctypes.get_last_error()
            if error != 1410:
                self.gdi32.DeleteObject(black_brush)
                self._class_brush = None
                raise ctypes.WinError(error)

        ex_style = (
            self.WS_EX_TOPMOST | self.WS_EX_TRANSPARENT | self.WS_EX_TOOLWINDOW
            | self.WS_EX_LAYERED | self.WS_EX_NOACTIVATE
        )
        self.hwnd = self.user32.CreateWindowExW(
            ex_style, self.class_name, 'Lunar Aim FOV', self.WS_POPUP,
            0, 0, 16, 16, None, None, hinstance, None
        )
        if not self.hwnd:
            self.gdi32.DeleteObject(black_brush)
            self._class_brush = None
            raise ctypes.WinError(ctypes.get_last_error())
        # Black is color-keyed to transparency; only the blue ring remains visible.
        if not self.user32.SetLayeredWindowAttributes(
            self.hwnd, 0x00000000, 255, self.LWA_COLORKEY
        ):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())
        self.user32.ShowWindow(self.hwnd, self.SW_HIDE)
        try:
            set_affinity = self.user32.SetWindowDisplayAffinity
            set_affinity.argtypes = [wintypes.HWND, wintypes.DWORD]
            set_affinity.restype = wintypes.BOOL
            # WDA_EXCLUDEFROMCAPTURE: keep our visible FOV ring out of DXGI captures.
            set_affinity(self.hwnd, 0x00000011)
        except Exception:
            pass

    def _make_wndproc(self):
        @self._wndproc_type
        def wndproc(hwnd, message, wparam, lparam):
            if message == self.WM_NCHITTEST:
                return self.HTTRANSPARENT
            if message == self.WM_MOUSEACTIVATE:
                return self.MA_NOACTIVATE
            if message == self.WM_ERASEBKGND:
                return 1
            if message == self.WM_PAINT:
                ps = _OverlayPAINTSTRUCT()
                hdc = self.user32.BeginPaint(hwnd, ctypes.byref(ps))
                rect = _OverlayRECT(0, 0, self.window_size, self.window_size)
                black = self.gdi32.CreateSolidBrush(0)
                self.user32.FillRect(hdc, ctypes.byref(rect), black)
                self.gdi32.DeleteObject(black)
                r, g, b = self.ring_color
                colorref = r | (g << 8) | (b << 16)
                pen = self.gdi32.CreatePen(self.PS_SOLID, self.ring_thickness, colorref)
                old_pen = self.gdi32.SelectObject(hdc, pen)
                old_brush = self.gdi32.SelectObject(
                    hdc, self.gdi32.GetStockObject(self.NULL_BRUSH)
                )
                pad = self.PADDING
                self.gdi32.Ellipse(
                    hdc, pad, pad,
                    max(pad + 1, self.window_size - pad),
                    max(pad + 1, self.window_size - pad)
                )
                # Four small ticks mark the exact center of the FOV circle.
                white_pen = self.gdi32.CreatePen(self.PS_SOLID, 1, 0x00FFFFFF)
                old_center_pen = self.gdi32.SelectObject(hdc, white_pen)
                mid = self.window_size // 2
                self.gdi32.MoveToEx(hdc, mid - 7, mid, None)
                self.gdi32.LineTo(hdc, mid - 3, mid)
                self.gdi32.MoveToEx(hdc, mid + 3, mid, None)
                self.gdi32.LineTo(hdc, mid + 7, mid)
                self.gdi32.MoveToEx(hdc, mid, mid - 7, None)
                self.gdi32.LineTo(hdc, mid, mid - 3)
                self.gdi32.MoveToEx(hdc, mid, mid + 3, None)
                self.gdi32.LineTo(hdc, mid, mid + 7)
                self.gdi32.SelectObject(hdc, old_center_pen)
                self.gdi32.DeleteObject(white_pen)
                self.gdi32.SelectObject(hdc, old_brush)
                self.gdi32.SelectObject(hdc, old_pen)
                self.gdi32.DeleteObject(pen)
                self.user32.EndPaint(hwnd, ctypes.byref(ps))
                return 0
            return self.user32.DefWindowProcW(hwnd, message, wparam, lparam)
        return wndproc

    def update_style(self, color=None, thickness=None):
        changed = False
        if color is not None:
            value = str(color).strip().lstrip('#')
            if len(value) == 6:
                try:
                    rgb = tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
                    if rgb != self.ring_color:
                        self.ring_color = rgb
                        changed = True
                except ValueError:
                    pass
        if thickness is not None:
            width = max(1, min(12, int(thickness)))
            if width != self.ring_thickness:
                self.ring_thickness = width
                changed = True
        if changed and self.hwnd and self.visible:
            self.user32.InvalidateRect(self.hwnd, None, True)
            self.user32.UpdateWindow(self.hwnd)

    def update_at_reticle(self, reticle_screen_x, reticle_screen_y, diameter, visible=True, color=None, thickness=None):
        """Draw the configurable FOV ring around the FOV center on the desktop."""
        self.update_style(color=color, thickness=thickness)
        diameter = max(0, min(700, int(round(float(diameter)))))
        visible = bool(visible) and diameter > 0
        if not self.hwnd:
            return
        if not visible:
            if self.visible:
                self.user32.ShowWindow(self.hwnd, self.SW_HIDE)
            self.visible = False
            self._last_geometry = None
            return

        size = diameter + self.PADDING * 2
        # Coordinates are the screen-space FOV center derived from the Debug
        # reticle plus the user's X/Y offset. Target coordinates are never used.
        x = int(round(float(reticle_screen_x) - size / 2.0))
        y = int(round(float(reticle_screen_y) - size / 2.0))
        geometry = (x, y, size, diameter)
        now = time.monotonic()
        geometry_changed = geometry != self._last_geometry or not self.visible
        topmost_refresh_due = now - self._last_topmost_refresh >= 1.0
        if geometry_changed or topmost_refresh_due:
            self.window_size = size
            self.diameter = diameter
            if not self.user32.SetWindowPos(
                self.hwnd, self.HWND_TOPMOST, x, y, size, size,
                self.SWP_NOACTIVATE | self.SWP_SHOWWINDOW
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            self.user32.ShowWindow(self.hwnd, self.SW_SHOWNOACTIVATE)
            if geometry_changed:
                self.user32.InvalidateRect(self.hwnd, None, True)
                self.user32.UpdateWindow(self.hwnd)
            self._last_geometry = geometry
            self._last_topmost_refresh = now
            if geometry_changed:
                print(
                    f'[INFO] FOV ring shown at screen ({x + size // 2}, '
                    f'{y + size // 2}), diameter={diameter}px.',
                    flush=True,
                )
        self.visible = True

    def pump(self):
        if not self.hwnd:
            return
        # MSG is opaque here; a correctly sized buffer is sufficient for PeekMessage.
        msg = ctypes.create_string_buffer(48 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
        while self.user32.PeekMessageW(msg, self.hwnd, 0, 0, self.PM_REMOVE):
            self.user32.TranslateMessage(msg)
            self.user32.DispatchMessageW(msg)

    def close(self):
        if self.hwnd:
            self.user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        try:
            self.user32.UnregisterClassW(self.class_name, self._hinstance)
        except Exception:
            pass
        if self._class_brush:
            try:
                self.gdi32.DeleteObject(self._class_brush)
            except Exception:
                pass
            self._class_brush = None


def get_primary_monitor_bounds():
    """Return primary-monitor bounds in physical desktop coordinates."""
    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ('cbSize', wintypes.DWORD),
            ('rcMonitor', _OverlayRECT),
            ('rcWork', _OverlayRECT),
            ('dwFlags', wintypes.DWORD),
        ]

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    try:
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.MonitorFromPoint.restype = wintypes.HANDLE
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        monitor = user32.MonitorFromPoint(wintypes.POINT(0, 0), 1)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            rect = info.rcMonitor
            if rect.right > rect.left and rect.bottom > rect.top:
                return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass

    return (
        0, 0,
        ctypes.windll.user32.GetSystemMetrics(0),
        ctypes.windll.user32.GetSystemMetrics(1),
    )


# Auto screen resolution
screensize = {
    'X': ctypes.windll.user32.GetSystemMetrics(0),
    'Y': ctypes.windll.user32.GetSystemMetrics(1),
}
screen_res_x = screensize['X']
screen_res_y = screensize['Y']
screen_x = screen_res_x // 2
screen_y = screen_res_y // 2

fov = 350
FF_GAIN = 0.85  # [PATCH] feedforward gain para o controlador NEW
confidence = 0.12

# Runtime Aim Assist and Triggerbot are controlled by the integrated menu.
mouse_methods = ['win32', 'ddxoft', 'makcu']
mouse_method = mouse_methods[0]
second_pc_ip = '192.67.67.67'


class MAKCU_UDP:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, msg):
        self.sock.sendto(msg.encode('utf-8'), (second_pc_ip, 5005))

    def click(self):
        self.send('CLICK:LEFT')

    def move(self, x, y):
        self.send(f'MOVE:{int(x)},{int(y)}')


class RawMouseInputMonitor:
    """Read physical mouse deltas through Windows Raw Input.

    Uses RIDEV_INPUTSINK so relative movement is available while another
    application owns focus. Only X/Y deltas are retained, in a short in-memory
    accumulator; no clicks, keys, coordinates, or device identifiers are saved.
    """

    WM_INPUT = 0x00FF
    WM_QUIT = 0x0012
    RID_INPUT = 0x10000003
    RIM_TYPEMOUSE = 0
    MOUSE_MOVE_ABSOLUTE = 0x0001
    RIDEV_INPUTSINK = 0x00000100
    RIDEV_REMOVE = 0x00000001
    HWND_MESSAGE = ctypes.c_void_p(-3)
    PM_NOREMOVE = 0x0000

    def __init__(self):
        self._lock = threading.Lock()
        self._dx = 0
        self._dy = 0
        self._thread = None
        self._thread_id = 0
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._available = False
        self._error = None
        self._class_name = 'LunarRawMouse_' + uuid.uuid4().hex
        self._user32 = None
        self._wndproc_ref = None
        self._hwnd = None

    @property
    def available(self):
        return bool(self._available)

    @property
    def error(self):
        return self._error

    def start(self):
        if self._thread and self._thread.is_alive():
            return self.available
        self._stop.clear()
        self._ready.clear()
        self._error = None
        with self._lock:
            self._dx = 0
            self._dy = 0
        self._thread = threading.Thread(
            target=self._run, name='LunarRawMouseInput', daemon=True
        )
        self._thread.start()
        self._ready.wait(1.5)
        return self.available

    def consume(self):
        """Return and clear accumulated physical relative mouse counts."""
        with self._lock:
            dx, dy = self._dx, self._dy
            self._dx = 0
            self._dy = 0
        return int(dx), int(dy)

    def stop(self):
        self._stop.set()
        user32 = self._user32
        thread_id = self._thread_id
        if user32 is not None and thread_id:
            try:
                user32.PostThreadMessageW(thread_id, self.WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.2)
        if self._thread and self._thread.is_alive():
            # Keep the callback and module references alive if Windows has not
            # yet delivered WM_QUIT; the worker must not call a freed WNDPROC.
            self._available = False
            return
        self._thread = None
        self._available = False
        self._thread_id = 0
        self._hwnd = None
        self._user32 = None
        self._wndproc_ref = None

    def _append_delta(self, dx, dy):
        # Filter corrupted/driver-glitch packets without affecting normal fast
        # flicks. Cumulative values are bounded to prevent unbounded backlog.
        if abs(dx) > 5000 or abs(dy) > 5000:
            return
        with self._lock:
            self._dx = max(-20000, min(20000, self._dx + int(dx)))
            self._dy = max(-20000, min(20000, self._dy + int(dy)))

    def _run(self):
        user32 = None
        kernel32 = None
        atom = 0
        hwnd = None
        registered = False
        try:
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            self._user32 = user32
            LRESULT = ctypes.c_ssize_t
            WPARAM = ctypes.c_size_t
            LPARAM = ctypes.c_ssize_t
            WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [
                    ('style', wintypes.UINT),
                    ('lpfnWndProc', WNDPROC),
                    ('cbClsExtra', ctypes.c_int),
                    ('cbWndExtra', ctypes.c_int),
                    ('hInstance', wintypes.HINSTANCE),
                    ('hIcon', wintypes.HICON),
                    ('hCursor', wintypes.HANDLE),
                    ('hbrBackground', wintypes.HBRUSH),
                    ('lpszMenuName', wintypes.LPCWSTR),
                    ('lpszClassName', wintypes.LPCWSTR),
                ]

            class MSG(ctypes.Structure):
                _fields_ = [
                    ('hwnd', wintypes.HWND), ('message', wintypes.UINT),
                    ('wParam', WPARAM), ('lParam', LPARAM),
                    ('time', wintypes.DWORD), ('pt', wintypes.POINT),
                ]

            class RAWINPUTDEVICE(ctypes.Structure):
                _fields_ = [
                    ('usUsagePage', wintypes.USHORT),
                    ('usUsage', wintypes.USHORT),
                    ('dwFlags', wintypes.DWORD),
                    ('hwndTarget', wintypes.HWND),
                ]

            class RAWINPUTHEADER(ctypes.Structure):
                _fields_ = [
                    ('dwType', wintypes.DWORD),
                    ('dwSize', wintypes.DWORD),
                    ('hDevice', wintypes.HANDLE),
                    ('wParam', WPARAM),
                ]

            user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
            user32.DefWindowProcW.restype = LRESULT
            user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
            user32.RegisterClassW.restype = wintypes.ATOM
            user32.CreateWindowExW.argtypes = [
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
            ]
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.RegisterRawInputDevices.argtypes = [
                ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT
            ]
            user32.RegisterRawInputDevices.restype = wintypes.BOOL
            user32.GetRawInputData.argtypes = [
                wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID,
                ctypes.POINTER(wintypes.UINT), wintypes.UINT,
            ]
            user32.GetRawInputData.restype = wintypes.UINT
            user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
            user32.GetMessageW.restype = ctypes.c_int
            user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
            user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
            user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, WPARAM, LPARAM]
            user32.PostThreadMessageW.restype = wintypes.BOOL
            user32.DestroyWindow.argtypes = [wintypes.HWND]
            user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD

            header_size = ctypes.sizeof(RAWINPUTHEADER)

            def _wndproc(hwnd_arg, message, wparam, lparam):
                if message == self.WM_INPUT:
                    try:
                        packet_size = wintypes.UINT(0)
                        raw_handle = ctypes.c_void_p(int(lparam) & ((1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1))
                        result = user32.GetRawInputData(
                            raw_handle, self.RID_INPUT, None,
                            ctypes.byref(packet_size), header_size,
                        )
                        if result == 0xFFFFFFFF or packet_size.value < header_size + 20:
                            return user32.DefWindowProcW(hwnd_arg, message, wparam, lparam)
                        buffer = ctypes.create_string_buffer(packet_size.value)
                        copied = user32.GetRawInputData(
                            raw_handle, self.RID_INPUT, buffer,
                            ctypes.byref(packet_size), header_size,
                        )
                        if copied != 0xFFFFFFFF and copied >= header_size + 20:
                            raw = buffer.raw
                            raw_type = struct.unpack_from('<I', raw, 0)[0]
                            if raw_type == self.RIM_TYPEMOUSE:
                                mouse_flags = struct.unpack_from('<H', raw, header_size)[0]
                                if not (mouse_flags & self.MOUSE_MOVE_ABSOLUTE):
                                    dx, dy = struct.unpack_from('<ii', raw, header_size + 12)
                                    self._append_delta(dx, dy)
                    except Exception:
                        pass
                return user32.DefWindowProcW(hwnd_arg, message, wparam, lparam)

            self._wndproc_ref = WNDPROC(_wndproc)
            instance = kernel32.GetModuleHandleW(None)
            window_class = WNDCLASSW()
            window_class.style = 0
            window_class.lpfnWndProc = self._wndproc_ref
            window_class.cbClsExtra = 0
            window_class.cbWndExtra = 0
            window_class.hInstance = instance
            window_class.hIcon = None
            window_class.hCursor = None
            window_class.hbrBackground = None
            window_class.lpszMenuName = None
            window_class.lpszClassName = self._class_name
            atom = user32.RegisterClassW(ctypes.byref(window_class))
            if not atom:
                raise ctypes.WinError(ctypes.get_last_error())

            hwnd = user32.CreateWindowExW(
                0, self._class_name, self._class_name, 0,
                0, 0, 0, 0, self.HWND_MESSAGE, None, instance, None,
            )
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._hwnd = hwnd
            self._thread_id = int(kernel32.GetCurrentThreadId())

            device = RAWINPUTDEVICE(0x01, 0x02, self.RIDEV_INPUTSINK, hwnd)
            if not user32.RegisterRawInputDevices(
                ctypes.byref(device), 1, ctypes.sizeof(RAWINPUTDEVICE)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            registered = True
            self._available = True
            self._ready.set()

            message = MSG()
            while not self._stop.is_set():
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self._error = f'{type(exc).__name__}: {exc}'
            self._available = False
            self._ready.set()
        finally:
            if registered and user32 is not None:
                try:
                    device = RAWINPUTDEVICE(0x01, 0x02, self.RIDEV_REMOVE, None)
                    user32.RegisterRawInputDevices(
                        ctypes.byref(device), 1, ctypes.sizeof(RAWINPUTDEVICE)
                    )
                except Exception:
                    pass
            if hwnd and user32 is not None:
                try:
                    user32.DestroyWindow(hwnd)
                except Exception:
                    pass
            if atom and user32 is not None and kernel32 is not None:
                try:
                    user32.UnregisterClassW(self._class_name, kernel32.GetModuleHandleW(None))
                except Exception:
                    pass
            self._available = False
            self._ready.set()


class Aimbot:
    mouse_dll = None
    makcu = None
    active_instance = None
    DEBUG_PANEL_WIDTH = 470

    if mouse_method.lower() == 'makcu':
        makcu = MAKCU_UDP()

    def __init__(self, box_constant=fov, collect_data=False, mouse_delay=0.0009):
        # Avoid OpenCV/PyTorch oversubscribing the CPU when inference and tracking
        # are active together. This does not change model weights, precision or imgsz.
        try:
            cv2.setUseOptimized(True)
            cv2.setNumThreads(2)
        except Exception:
            pass
        try:
            cpu_count = max(1, int(os.cpu_count() or 4))
            torch.set_num_threads(max(1, min(2, cpu_count // 3)))
            try:
                torch.set_num_interop_threads(1)
            except RuntimeError:
                # PyTorch may already have initialized its inter-op pool.
                pass
        except Exception:
            pass
        # The capture and detector use fixed, repeated tensor shapes. cuDNN's
        # benchmark selection can reduce convolution overhead on the CUDA path.
        try:
            if torch.cuda.is_available():
                torch.backends.cudnn.enabled = True
                torch.backends.cudnn.benchmark = True
        except Exception:
            pass

        self.base_box_constant = int(box_constant)
        self.box_constant = int(box_constant)
        # Capture dimensions may be rectangular on widescreen monitors.
        self.capture_width = int(box_constant)
        self.capture_height = int(box_constant)
        self._capture_bounds = None
        self._max_capture_size = None
        # Prefer DXGI Desktop Duplication (DXcam); keep MSS as a safe fallback.
        self._dxcam_camera = None
        self._mss_camera = None
        self._capture_backend_name = 'UNINITIALIZED'
        self._capture_backend_error = None
        self._capture_first_frame = None
        self.collect_data = collect_data
        self.mouse_delay = mouse_delay

        # Runtime master switch is controlled by the menu's configurable
        # Feature Toggle Key. Aim Assist and Triggerbot retain independent
        # enable flags loaded from lunar_menu_settings.json.
        self.master_enabled = False
        self.aim_enabled = False
        self.trigger_enabled = False
        self.trigger_held = False
        self.trigger_candidate_since = None

        # Numpad 1 opens the normal Debug; Numpad 2 opens a second Debug
        # view that is recorded to the user's Videos folder until it is closed.
        self.debug_mode = False
        self.debug_record_mode = False
        self.numpad1_was_down = False
        self.numpad2_was_down = False
        self._debug_toggle_events = queue.SimpleQueue()
        self._debug_record_toggle_events = queue.SimpleQueue()
        self._debug_hotkey_stop = threading.Event()
        self._debug_hotkey_thread = None
        self._debug_hotkey_thread_id = None
        self._debug_keyboard_hook = None
        self._debug_hook_proc = None
        self._debug_hotkey_ready = threading.Event()
        self._debug_hotkey_error = None
        self._debug_hook_available = False
        self._debug_physical_key_down = False
        self.debug_window_name = 'Lunar Debug - Screen Capture'
        self.debug_window_created = False
        self.debug_record_window_name = 'Lunar Debug Recording - Screen Capture'
        self.debug_record_window_created = False
        # Numpad 3 opens a live screen-space ignore-region editor.
        # It is independent of local-game memory and uses only captured pixels.
        self.self_id_mode = False  # retained as the editor-open state for compatibility
        self.self_id_window_name = 'Lunar - Ignore Region Editor (Numpad 3)'
        self.self_id_window_created = False
        self._ignore_region_norm = None  # normalized (left, top, right, bottom)
        self._ignore_region_path = None
        self._ignore_region_drag_mode = None
        self._ignore_region_drag_start = None
        self._ignore_region_drag_origin = None
        self._ignore_region_resize_sides = ()
        self._ignore_region_status = 'Arraste para criar a regiao; arraste dentro para mover.'
        self._self_id_toggle_events = queue.SimpleQueue()
        self.numpad3_was_down = False
        self._self_id_snapshot_frame = None
        self._self_id_snapshot_detections = []
        self._self_id_snapshot_updated_at = 0.0
        self._self_id_snapshot_interval = 0.10
        self._self_id_selected_detection = None
        self._self_id_selection_hits = []
        self._self_id_selection_cycle = 0
        self._self_id_last_click = None
        self._self_id_last_click_at = 0.0
        self._self_id_ui_buttons = {}
        self._self_id_active_reference = 0
        self._self_id_references = []
        self._self_id_hist_centered = np.empty((0, 48), dtype=np.float32)
        self._self_id_hist_norms = np.empty((0,), dtype=np.float32)
        self._self_id_gray_matrix = np.empty((0, 24 * 48), dtype=np.float32)
        self._self_id_match_threshold = 0.80
        self._self_id_status = 'Clique em uma caixa para selecionar seu personagem.'
        self._self_id_path = None
        # Temporal identity tracker initialized by the user's Numpad-3 selection.
        # It combines appearance, motion continuity and box scale; static skin
        # similarity alone is never treated as proof when multiple candidates tie.
        self._self_track_box = None
        self._self_track_center = None
        self._self_track_size = None
        self._self_track_velocity = (0.0, 0.0)
        self._self_track_last_seen = 0.0
        self._self_track_hits = 0
        self._self_track_misses = 0
        self._self_track_state = 'UNSET'
        self._self_track_score = 0.0
        self._self_track_last_adapt = 0.0
        self._self_online_descriptors = []
        self._self_track_last_candidate = None
        self._debug_recording_writer = None
        self._debug_recording_path = None
        self._debug_recording_size = None
        # The Debug UI is intentionally capped; detection/aiming keep their own loop rate.
        self._debug_preview_fps_limit = 18.0
        self._debug_last_render_at = 0.0
        self._debug_last_display_at = None
        self._debug_preview_fps = 0.0
        # Record at the same cadence as the Debug preview so playback duration stays accurate.
        self._debug_recording_fps = 18.0
        self._debug_recording_next_frame_at = None

        # Calibrated aim-point location, stored relative to the captured crop.
        self.crosshair_position_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'config', 'debug_crosshair_position.json'
        )
        self.crosshair_x = (self.capture_width - 1) / 2.0
        self.crosshair_y = (self.capture_height - 1) / 2.0
        self._calibrated_reticle_offset_x = 0.0
        self._calibrated_reticle_offset_y = 0.0
        self._current_detection_box = None
        self._load_crosshair_position()

        # Custom Aim Assist anchor: a draggable blue point placed on a YOLO
        # detection in either Debug view. Coordinates are normalized
        # inside the detected box so the point scales with near/far targets.
        self.aim_point_config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'config', 'aim_point_calibration.json'
        )
        self.aim_point_u = 0.50
        self.aim_point_v = 0.09  # default near the center of the head region
        self._aim_point_dragging = False
        self._aim_point_drag_box_center = None
        self._debug_boxes_cache = []
        self._load_aim_point_calibration()
        # Aim target mode is selected in the menu: ALL uses the custom hitbox;
        # HEAD uses this draggable normalized point within each live detection.
        self.aim_target_mode = 'all'

        self._debug_direction_down = {
            'left': False, 'right': False, 'up': False, 'down': False
        }
        self._debug_direction_repeat_at = {
            'left': 0.0, 'right': 0.0, 'up': 0.0, 'down': 0.0
        }
        self._debug_reset_was_down = False

        # Shared settings file owned by the standalone menu controller.
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.project_root = project_root
        self.lib_dir = os.path.join(project_root, 'lib')
        self.menu_settings_path = os.path.join(project_root, 'lunar_menu_settings.json')
        self.friend_dir = os.path.join(project_root, 'friends')
        self.friend_references_path = os.path.join(self.friend_dir, 'friend_references.json')
        self.friend_preview_path = os.path.join(self.friend_dir, 'friend_preview.png')
        self.friend_capture_frame_path = os.path.join(self.friend_dir, 'friend_capture_frame.png')
        self._self_id_path = os.path.join(project_root, 'self_player_references.json')
        # Numpad 3 has been replaced by a spatial exclusion region. Old local-avatar
        # appearance references remain on disk for rollback, but are not applied.
        self._self_id_references = []
        self._rebuild_self_player_reference_cache()
        self._ignore_region_path = os.path.join(project_root, 'aim_ignore_region.json')
        self._load_ignore_region()
        self.friend_capture_binding = {'type': 'keyboard', 'vk': 0x2E}  # DELETE by default
        self.friend_filter_enabled = True
        self._friend_reference_templates = []
        self._friend_reference_mtime_ns = None
        self._friend_capture_pending = False
        self._menu_config_mtime_ns = None
        self._menu_last_check = 0.0
        self._menu_settings_lock = threading.RLock()
        self.aim_fov_size = 100  # circular FOV diameter in captured-frame pixels
        self.aim_fov_enabled = True  # acquisition boundary stays active
        self.aim_fov_visible = True  # visual ring only; independent of acquisition
        self.aim_fov_thickness = 3
        self.aim_fov_color = '#419DFF'
        self.aim_fov_offset_x = 0
        self.aim_fov_offset_y = 0
        # Independent Aim Assist hitbox edge offsets, as percentages of the
        # raw detector box size. Negative moves an edge left/up; positive moves
        # it right/down. Zero keeps the original detector edge.
        self.aim_hitbox_left_offset = 0
        self.aim_hitbox_right_offset = 0
        self.aim_hitbox_top_offset = 0
        self.aim_hitbox_bottom_offset = 0
        # Derived legacy dimensions retained for existing diagnostics.
        self.aim_hitbox_width = 100
        self.aim_hitbox_height = 100
        self.manual_input_priority = False  # [PATCH] para de auto-cancelar aim
        self._manual_mouse_dx = 0
        self._manual_mouse_dy = 0
        self._manual_mouse_last_at = 0.0
        self._manual_mouse_active = False
        self._raw_mouse_input = RawMouseInputMonitor()
        self._raw_mouse_input_available = False
        self._fov_settings_dirty = True
        # Normalized controller controls: 0.10..1.00.
        # Speed controls how quickly the tracking controller follows; Force
        # controls how much of the current position error is corrected.
        self.aim_speed = 0.75
        self.aim_smooth = self.aim_speed  # legacy alias
        self.aim_strength = 0.75
        self.confidence_threshold = float(confidence)
        self.conf = self.confidence_threshold
        self.detector_model = 'universal'
        self._loaded_detector_model = None
        self.aim_method = 'new'  # NEW uses damped control; OLD remains available as fallback
        # W2.0 TEST: when enabled, Aim Assist movement requires a physical
        # left-mouse hold. It does not make the Trigger Bot click count as input.
        self.aim_hold_lmb = False
        self.trigger_box_left = 50
        self.trigger_box_right = 50
        self.trigger_box_top = 50
        self.trigger_box_bottom = 50
        self.trigger_box_size = 100  # legacy aggregate for compatibility/logging
        self.trigger_delay_ms = 50
        self.dot_color_on = '#35D49A'
        self.dot_color_off = '#E45D70'
        self.dot_corner = 'bottom_right'
        self.dot_size = 12
        self.activation_binding = {'type': 'mouse', 'vk': 0x04}
        self._activation_events = queue.SimpleQueue()
        self._activation_stop = threading.Event()
        self._activation_thread = None
        # No artificial activation delay: control still waits for a fresh,
        # detector-confirmed target before producing any mouse input.
        self._aim_activation_warmup_until = 0.0
        self._aim_first_lock_at = 0.0
        self._aim_first_lock_target_id = None

        # Target lock + movement state.
        self._aim_remainder_x = 0.0
        self._aim_remainder_y = 0.0
        self._aim_last_update_time = None
        self._stable_prev_error_x = None
        self._stable_prev_error_y = None
        self._stable_inside_deadzone = False
        # Fresh output worker: inference publishes one small relative correction
        # per frame. New frames replace a not-yet-dispatched correction.
        self._aim_motion_lock = threading.Lock()
        self._aim_motion_wakeup = threading.Event()
        self._aim_motion_stop = threading.Event()
        self._aim_motion_pending = None
        self._aim_motion_sequence = 0
        self._aim_motion_thread = None
        self._aim_motion_timer_handle = None
        self._aim_motion_timer_kernel32 = None
        self._aim_motion_timer_high_resolution = False
        # Shared decision/output telemetry. Both Debug windows read this exact
        # snapshot so they explain why the same frame did or did not move.
        self._aim_diag_lock = threading.Lock()
        self._aim_diagnostics = {
            'decision': 'BLOCKED', 'reason': 'STARTUP', 'target_id': None,
            'error_x': 0.0, 'error_y': 0.0, 'distance': 0.0,
            'response_rate': 0.0, 'approach': 0.0, 'tracking_weight': 0.0,
            'command_dx': 0, 'command_dy': 0, 'queue_state': 'EMPTY',
            'queue_steps': 0, 'output_state': 'IDLE', 'output_reason': 'STARTUP',
            'last_dx': 0, 'last_dy': 0, 'last_output_at': 0.0,
            'last_command_at': 0.0, 'last_command_expires_at': 0.0,
            'worker_state': 'STARTING',
        }
        # Closed-loop mouse-to-screen calibration. The estimator learns the
        # game's actual pixels-per-count independently on each axis.
        self._adaptive_feedback_lock = threading.Lock()
        self._adaptive_output_history = []
        self._camera_flow_history = []
        self._adaptive_gain_samples_x = []
        self._adaptive_gain_samples_y = []
        # Conservative startup estimate prevents a first-frame dash on high-sensitivity games.
        # It is replaced by measured global-scene-flow feedback once several clean samples exist.
        self._adaptive_gain_x = 3.0
        self._adaptive_gain_y = 3.0
        self._adaptive_gain_ready_x = False
        self._adaptive_gain_ready_y = False
        self._adaptive_latency_s = 0.050
        self._adaptive_prev_error_x = None
        self._adaptive_prev_error_y = None
        self._adaptive_prev_observation_time_x = None
        self._adaptive_prev_observation_time_y = None
        self._adaptive_prev_track_id_x = None
        self._adaptive_prev_track_id_y = None
        self._stable_prev_error_x = None
        self._stable_prev_error_y = None
        self._stable_inside_deadzone = False
        self._new_ctrl_last_time = None
        self._new_ctrl_prev_error_x = None
        self._new_ctrl_prev_error_y = None
        self._new_ctrl_error_rate_x = 0.0
        self._new_ctrl_error_rate_y = 0.0
        self._new_ctrl_velocity_x = 0.0
        self._new_ctrl_velocity_y = 0.0
        self._new_ctrl_remainder_x = 0.0
        self._new_ctrl_remainder_y = 0.0
        self._aim_locked_box = None
        self._aim_locked_part = None
        # The anchor mode records whether acquisition began at an edge or inside.
        # Its normalized U/V point remains attached to this target until the
        # player deliberately repositions it with physical mouse movement.
        self._aim_anchor_mode = None
        # Keep the user's chosen point as normalized coordinates within the
        # resized hitbox. It follows the detected body instead of freezing at
        # the screen-space crosshair.
        self._aim_point_u = None
        self._aim_point_v = None
        self._aim_locked_hitbox = None
        self._aim_locked_track_id = None
        self._aim_locked_class_id = None
        self._aim_lock_misses = 0
        self._aim_lock_lost_since = None
        self._aim_lock_wait_manual_retarget = False
        self._aim_last_valid_measurement_time = None
        self._filtered_aim_x = None
        self._filtered_aim_y = None
        # Alpha-beta tracking state: retain one target and its relative hitbox
        # anchor across noisy YOLO frames instead of reacquiring every frame.
        self._aim_track_center_x = None
        self._aim_track_center_y = None
        self._aim_track_velocity_x = 0.0
        self._aim_track_velocity_y = 0.0
        self._aim_track_last_time = None
        self._aim_point_velocity_x = 0.0
        self._aim_point_velocity_y = 0.0
        self._aim_point_last_time = None
        self._aim_track_state = 'IDLE'
        self._aim_point_filter_x = None
        self._aim_point_filter_y = None
        self._detection_tracks = []
        self._next_detection_track_id = 1
        # Global optical-flow estimate from the previous captured frame to the
        # current one. It compensates screen-space tracking when the player pans
        # the camera manually or the aim controller moves the view.
        self._camera_prev_gray = None
        self._camera_motion_affine = None
        self._camera_motion_dx = 0.0
        self._camera_motion_dy = 0.0
        self._camera_motion_quality = 0.0
        self._camera_motion_inliers = 0
        self._camera_motion_cells = 0
        self._camera_motion_reject_reason = 'STARTUP'
        self._camera_motion_coverage = 0.0
        self._camera_motion_residual = 0.0
        self._camera_motion_model = 'NONE'
        self._camera_phase_window_cache = {}
        self._debug_detection_meta = []
        self._detector_size_fallback_logged = False
        self._detector_zoom_warning_logged = False
        self._target_confidence_floor = 0.07
        # Start at 512px to reduce per-frame GPU work. Promote to 640/768 only
        # when measured inference latency proves there is sustained headroom.
        self._detector_imgsz = 512
        self._detector_latency_ema_ms = 0.0
        self._detector_slow_streak = 0
        self._detector_fast_streak = 0
        self._last_detector_pass_ms = 0.0
        self._last_base_inference_ms = 0.0
        self._last_detector_imgsz = 512
        self._last_inference_scale = 'BASE'
        self._last_zoom_used = False
        self._last_zoom_reason = 'not needed'
        self._last_zoom_run_at = 0.0
        # Empty-scene ROI scans are less frequent to protect steady-state FPS;
        # weak detections still receive a quicker targeted refinement.
        self._zoom_interval_empty_scene = 0.65
        self._zoom_interval_uncertain_target = 0.24
        self._last_inference_ms = 0.0
        self._last_capture_ms = 0.0
        self._last_postprocess_ms = 0.0
        self._last_frame_age_ms = 0.0
        self._head_inference_ms = 0.0
        self._head_inference_last_at = 0.0
        # Secondary head refinement is asynchronous on CPU. A short refresh
        # interval plus a two-ROI batch keeps the locked target and next candidate
        # supplied with recent measurements without a second GPU model.
        self._head_inference_interval = 0.12
        self._head_last_locked_track_id = None
        self._head_worker_lock = threading.Lock()
        self._head_worker_wakeup = threading.Event()
        self._head_worker_stop = threading.Event()
        self._head_worker_pending = None
        self._head_worker_latest = None
        self._head_worker_thread = None
        self._head_worker_busy = False
        self._head_worker_next_job_id = 0
        self._head_worker_consumed_job_id = 0
        self._head_worker_last_ms = 0.0
        self._head_worker_last_error = ''
        self._head_worker_device = 'cpu'
        self._last_camera_flow_ms = 0.0
        self._camera_motion_last_update_at = 0.0
        self._camera_motion_min_interval = 1.0 / 45.0
        self._head_cache_age_ms = 0.0
        self._recognized_target_ids = set()
        self._runtime_fps = 0.0
        self._runtime_prev_frame_at = None
        self._last_head_raw_point = None
        self._last_head_corrected_point = None
        self._last_effective_confidence = 0.12
        self._resource_sample_at = 0.0
        self._process_ram_mb = -1.0
        self._cuda_allocated_mb = 0.0
        self._cuda_reserved_mb = 0.0
        # Adaptive damping state: reduce gain after a detected overshoot and
        # soften corrections while the player is moving with keyboard controls.
        self._aim_previous_error_x = None
        self._aim_previous_error_y = None
        self._aim_axis_gain_x = 1.0
        self._aim_axis_gain_y = 1.0
        self._last_movement_input = False
        self._last_aim_gain_factor = 1.0
        self._last_aim_speed_factor = 1.0

        self._refresh_menu_settings(force=True)
        self._load_friend_references(force=True)

        self.status_overlay = TriggerStatusOverlay()
        self.status_overlay.update_appearance(
            self.dot_color_on, self.dot_color_off, self.dot_corner, self.dot_size
        )
        self.status_overlay.set_enabled(False)
        # Configurable click-through FOV ring. It is visible on the game monitor
        # and shares the exact center/radius used by Aim Assist acquisition.
        self.aim_fov_overlay = None
        try:
            self.aim_fov_overlay = AimFOVOverlay()
            print('[INFO] Configurable desktop Aim FOV overlay initialized.', flush=True)
        except Exception as exc:
            print(f'[WARN] Desktop FOV overlay unavailable; Debug preview remains available: {exc}', flush=True)
        self.stop_event_handle = None
        stop_event_name = os.environ.get('LUNAR_STOP_EVENT_NAME')
        if stop_event_name:
            kernel32 = ctypes.windll.kernel32
            kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
            kernel32.OpenEventW.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            self.stop_event_handle = kernel32.OpenEventW(0x00100000, False, stop_event_name)
            if not self.stop_event_handle:
                raise ctypes.WinError(ctypes.get_last_error())
        Aimbot.active_instance = self

        print('[INFO] Loading the neural network model')
        self.model = self._load_primary_detector()

        # Aim Assist now uses the complete Universal character bounding box.
        # A secondary head-only model is intentionally not loaded: it must not
        # impose a fixed head anchor or add inference delay to the aim loop.
        self.head_model = None
        self.head_model_ready = False
        self.head_model_path = os.path.join(self.lib_dir, 'v8s-head.pt')
        self.head_model_conf = 0.10
        self.head_model_imgsz = 640
        self._head_model_error_logged = False
        self._head_model_force_cpu = True
        self._head_point_cache = {}
        self._debug_head_points = {}

        if torch.cuda.is_available():
            print(colored('CUDA ACCELERATION [ENABLED]', 'green'))
        else:
            print(colored('[!] CUDA ACCELERATION IS UNAVAILABLE', 'red'))
            print(colored('[!] Check your PyTorch installation, else performance will be poor', 'red'))

        self.conf = float(self.confidence_threshold)
        self.iou = 0.45
        self.mouse_method = mouse_method

        if self.mouse_method.lower() == 'ddxoft':
            dll_path = os.path.abspath('lib/mouse/dd40605x64.dll')
            if not os.path.exists(dll_path):
                raise FileNotFoundError(f'ddxoft DLL not found at {dll_path}')
            Aimbot.mouse_dll = ctypes.WinDLL(dll_path)
            time.sleep(1)
            Aimbot.mouse_dll.DD_btn.argtypes = [ctypes.c_int]
            Aimbot.mouse_dll.DD_btn.restype = ctypes.c_int
            if Aimbot.mouse_dll.DD_btn(0) != 1:
                print('Failed to initialize ddxoft mouse. Defaulting to Win32')
                self.mouse_method = 'win32'
            else:
                print(colored('Loaded ddxoft successfully!', 'green'))

        print('\n[INFO] MENU CONTROLS AIM ASSIST / TRIGGERBOT SETTINGS')
        print('[INFO] Feature Toggle Key: configured in the Lunar menu (keyboard or mouse).')
        print(f'[INFO] Friend capture key: {self.friend_capture_binding["type"]} VK={self.friend_capture_binding["vk"]}')
        print('[INFO] NUMPAD 1: normal Debug | NUMPAD 2: recorded Debug | NUMPAD 3: Ignore Region Editor.')
        print('[INFO] Numpad 3: drag to create/move a screen-space exclusion region; CTRL+drag resizes; auto-save.')
        print('[INFO] Debug preview capped at 18 FPS; runtime detection and aiming are not capped.')
        print('[INFO] Performance: adaptive YOLO resolution 512/640/768; ROI zoom is cadence-limited; head refinement runs asynchronously on CPU.')
        print('[INFO] Mouse output uses 2-count micro-steps with a 10-22 ms frame-aware window; total delta is preserved.')
        print('[INFO] Debug calibration: NUMPAD 4/6/8/2 or arrows; SHIFT = 10 px; NUMPAD 5 = center')
        print('[INFO] Tracking Speed / Aim Force: 0.10-1.00 (strong nonlinear response).')
        print('[INFO] Aim FOV diameter: 0-700 px; Trigger hitbox: 0-200%; delay: 0-1000 ms')
        print('[INFO] FOV ring is drawn over the captured YOLO frame in either Debug window.')
        print('[INFO] Menu settings reload automatically from lunar_menu_settings.json.')
        print('[INFO] DEL (default Friend Capture key) only captures references; it does not close the app.')
        print('[INFO] Use Exit in the tray icon to close safely.')
        self._start_activation_watcher()

    def _rebuild_self_player_reference_cache(self):
        """Precompute vectorized global and spatial appearance references."""
        hist_rows = []
        gray_rows = []
        spatial_rows = []
        spatial_valid = []
        for reference in getattr(self, '_self_id_references', []):
            try:
                hist = np.asarray(reference.get('hist', []), dtype=np.float32).reshape(-1)
                gray = np.asarray(reference.get('gray', []), dtype=np.float32).reshape(-1)
                if hist.size != 48 or gray.size != 24 * 48:
                    continue
                spatial = np.asarray(reference.get('spatial_hist', []), dtype=np.float32).reshape(-1)
                hist_rows.append(hist)
                gray_rows.append(gray)
                if spatial.size == 192:
                    spatial_rows.append(spatial)
                    spatial_valid.append(True)
                else:
                    spatial_rows.append(np.zeros((192,), dtype=np.float32))
                    spatial_valid.append(False)
            except (TypeError, ValueError):
                continue

        if not hist_rows:
            self._self_id_hist_centered = np.empty((0, 48), dtype=np.float32)
            self._self_id_hist_norms = np.empty((0,), dtype=np.float32)
            self._self_id_gray_matrix = np.empty((0, 24 * 48), dtype=np.float32)
            self._self_id_spatial_matrix = np.empty((0, 192), dtype=np.float32)
            self._self_id_spatial_valid = np.empty((0,), dtype=bool)
            return

        hist_matrix = np.ascontiguousarray(np.vstack(hist_rows), dtype=np.float32)
        centered = hist_matrix - hist_matrix.mean(axis=1, keepdims=True)
        norms = np.linalg.norm(centered, axis=1)
        self._self_id_hist_centered = np.ascontiguousarray(centered, dtype=np.float32)
        self._self_id_hist_norms = np.maximum(norms, 1e-8).astype(np.float32, copy=False)
        self._self_id_gray_matrix = np.ascontiguousarray(np.vstack(gray_rows), dtype=np.float32)
        self._self_id_spatial_matrix = np.ascontiguousarray(np.vstack(spatial_rows), dtype=np.float32)
        self._self_id_spatial_valid = np.asarray(spatial_valid, dtype=bool)

    def _load_self_player_references(self):
        """Load manually selected local-player appearance references."""
        try:
            with open(self._self_id_path, 'r', encoding='utf-8') as stream:
                data = json.load(stream)
            references = data.get('references', []) if isinstance(data, dict) else []
            loaded = []
            for item in references:
                if not isinstance(item, dict):
                    continue
                hist = np.asarray(item.get('hist', []), dtype=np.float32)
                gray = np.asarray(item.get('gray', []), dtype=np.float32)
                if hist.size != 8 * 6 or gray.size != 24 * 48:
                    continue
                hist = hist.reshape(8, 6)
                gray = gray.reshape(48, 24)
                spatial = np.asarray(item.get('spatial_hist', []), dtype=np.float32).reshape(-1)
                loaded.append({
                    'id': str(item.get('id') or uuid.uuid4().hex[:8]),
                    'label': str(item.get('label') or f'Vista {len(loaded) + 1}')[:32],
                    'hist': hist.reshape(-1).tolist(),
                    'spatial_hist': spatial.tolist() if spatial.size == 192 else [],
                    'gray': gray.reshape(-1).tolist(),
                    'quality': float(item.get('quality', 1.0)),
                    'source_size': item.get('source_size', [0, 0]),
                })
            self._self_id_references = loaded[:64]
            active_reference = int(data.get('active_reference', 0)) if isinstance(data, dict) else 0
            self._self_id_active_reference = min(
                max(0, active_reference),
                max(0, len(self._self_id_references) - 1),
            )
            self._rebuild_self_player_reference_cache()
            print(
                f'[INFO] Local-player references loaded: {len(self._self_id_references)}',
                flush=True,
            )
        except FileNotFoundError:
            self._self_id_references = []
            self._rebuild_self_player_reference_cache()
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._self_id_references = []
            self._rebuild_self_player_reference_cache()
            print(f'[WARN] Could not load local-player references: {exc}', flush=True)

    def _save_self_player_references(self):
        """Persist local-player references atomically; never overwrites on load."""
        data = {
            'version': 1,
            'active_reference': int(self._self_id_active_reference),
            'match_threshold': float(self._self_id_match_threshold),
            'references': self._self_id_references,
        }
        try:
            os.makedirs(os.path.dirname(self._self_id_path), exist_ok=True)
            temporary = self._self_id_path + '.tmp'
            with open(temporary, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, ensure_ascii=False, separators=(',', ':'))
            os.replace(temporary, self._self_id_path)
            self._rebuild_self_player_reference_cache()
            return True
        except OSError as exc:
            self._self_id_status = f'Falha ao salvar: {exc}'
            print(f'[WARN] Could not save local-player references: {exc}', flush=True)
            return False

    @staticmethod
    def _make_self_player_descriptor(frame, box):
        """Build a pose-tolerant appearance signature for local-player re-identification.

        The descriptor combines a global HSV histogram with four vertical color
        bands. The bands preserve clothing/color layout, while the global
        histogram remains tolerant to jumping, running and different poses.
        Existing JSON references without ``spatial_hist`` remain compatible.
        """
        if frame is None or getattr(frame, 'ndim', 0) < 3 or frame.size == 0:
            return None
        frame_h, frame_w = frame.shape[:2]
        try:
            x1, y1, x2, y2 = (int(round(float(value))) for value in box)
        except (TypeError, ValueError):
            return None
        if x2 < x1:
            x1, x2 = x2, x1
        if y2 < y1:
            y1, y2 = y2, y1
        if x2 == x1:
            x2 += 1
        if y2 == y1:
            y2 += 1
        width = max(1, x2 - x1)
        height = max(1, y2 - y1)
        pad_x = max(1, int(round(width * 0.035)))
        pad_y = max(1, int(round(height * 0.025)))
        left = max(0, x1 - pad_x)
        top = max(0, y1 - pad_y)
        right = min(frame_w, x2 + pad_x)
        bottom = min(frame_h, y2 + pad_y)
        if right <= left or bottom <= top:
            return None
        crop = frame[top:bottom, left:right]
        if crop.size == 0:
            return None

        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [8, 6], [0, 180, 0, 256])
        cv2.normalize(hist, hist, alpha=1.0, norm_type=cv2.NORM_L1)

        # Coarse spatial bins distinguish clothing arrangement without tying
        # identity to a single exact pose. Each band has its own normalized HSV hist.
        spatial_parts = []
        crop_h = hsv.shape[0]
        for band in range(4):
            y0 = int(round(band * crop_h / 4.0))
            y1b = int(round((band + 1) * crop_h / 4.0))
            band_img = hsv[y0:max(y0 + 1, y1b), :]
            band_hist = cv2.calcHist([band_img], [0, 1], None, [8, 6], [0, 180, 0, 256])
            cv2.normalize(band_hist, band_hist, alpha=1.0, norm_type=cv2.NORM_L1)
            spatial_parts.append(band_hist.reshape(-1))
        spatial_hist = np.concatenate(spatial_parts).astype(np.float32, copy=False)

        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        interpolation = cv2.INTER_AREA if gray.shape[0] >= 48 and gray.shape[1] >= 24 else cv2.INTER_CUBIC
        gray = cv2.resize(gray, (24, 48), interpolation=interpolation)
        crop_area = int(crop.shape[0] * crop.shape[1])
        quality = max(0.05, min(1.0, (crop_area / float(24 * 48)) ** 0.5))
        return {
            'hist': hist.astype(np.float32).reshape(-1).tolist(),
            'spatial_hist': spatial_hist.tolist(),
            'gray': (gray.astype(np.float32) / 255.0).reshape(-1).tolist(),
            'quality': round(float(quality), 3),
            'source_size': [int(crop.shape[1]), int(crop.shape[0])],
        }

    @staticmethod
    def _compare_self_player_descriptors(candidate, reference):
        """Compare identity using color layout first, grayscale texture second."""
        try:
            candidate_hist = np.asarray(candidate['hist'], dtype=np.float32).reshape(48)
            reference_hist = np.asarray(reference['hist'], dtype=np.float32).reshape(48)
            intersection = float(np.minimum(candidate_hist, reference_hist).sum())
            global_score = max(0.0, min(1.0, intersection))
            candidate_spatial = np.asarray(candidate.get('spatial_hist', []), dtype=np.float32).reshape(-1)
            reference_spatial = np.asarray(reference.get('spatial_hist', []), dtype=np.float32).reshape(-1)
            if candidate_spatial.size == 192 and reference_spatial.size == 192:
                spatial_score = max(0.0, min(1.0, float(np.minimum(candidate_spatial, reference_spatial).sum()) / 4.0))
                color_score = 0.58 * global_score + 0.42 * spatial_score
            else:
                color_score = global_score
            candidate_gray = np.asarray(candidate['gray'], dtype=np.float32).reshape(48, 24)
            reference_gray = np.asarray(reference['gray'], dtype=np.float32).reshape(48, 24)
            gray_score = max(0.0, min(1.0, 1.0 - float(np.mean(np.abs(candidate_gray - reference_gray)))))
            return 0.86 * color_score + 0.14 * gray_score
        except (KeyError, TypeError, ValueError, cv2.error):
            return 0.0

    @staticmethod
    def _self_box_iou(box_a, box_b):
        ax1, ay1, ax2, ay2 = map(float, box_a)
        bx1, by1, bx2, by2 = map(float, box_b)
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
        inter = iw * ih
        area_a = max(1.0, ax2 - ax1) * max(1.0, ay2 - ay1)
        area_b = max(1.0, bx2 - bx1) * max(1.0, by2 - by1)
        return inter / max(1e-6, area_a + area_b - inter)

    def _reset_self_player_track(self, box=None, descriptor=None):
        """Start/restart the selected local-avatar track from a manual selection."""
        if box is None:
            self._self_track_box = None
            self._self_track_center = None
            self._self_track_size = None
            self._self_track_velocity = (0.0, 0.0)
            self._self_track_last_seen = 0.0
            self._self_track_hits = 0
            self._self_track_misses = 0
            self._self_track_state = 'UNSET'
            self._self_track_score = 0.0
            self._self_track_last_adapt = 0.0
            self._self_online_descriptors = []
            self._self_track_last_candidate = None
            return
        x1, y1, x2, y2 = map(float, box)
        self._self_track_box = (x1, y1, x2, y2)
        self._self_track_center = ((x1 + x2) * 0.5, (y1 + y2) * 0.5)
        self._self_track_size = (max(1.0, x2 - x1), max(1.0, y2 - y1))
        self._self_track_velocity = (0.0, 0.0)
        self._self_track_last_seen = time.perf_counter()
        self._self_track_hits = 1
        self._self_track_misses = 0
        self._self_track_state = 'VERIFYING'
        self._self_track_score = 1.0
        self._self_track_last_adapt = 0.0
        self._self_online_descriptors = [descriptor] if descriptor is not None else []
        self._self_track_last_candidate = None

    def _classify_self_player_detections(self, frame, boxes, indices=None):
        """Track the selected avatar across pose changes, jumps and short dashes.

        Static references produce appearance scores. A temporal track then adds
        predicted location and scale continuity. Large displacements may reacquire
        only when appearance is a strong, unique match; ties become AMBIGUOUS
        rather than accidentally marking another player with the same skin.
        """
        flags = [False] * len(boxes)
        scores = [0.0] * len(boxes)
        if not self._self_id_references and not self._self_online_descriptors:
            self._self_track_state = 'UNSET'
            return flags, scores

        selected_indices = list(range(len(boxes)) if indices is None else indices)
        selected_indices = [int(i) for i in selected_indices if 0 <= int(i) < len(boxes)]
        if len(selected_indices) > 64:
            selected_indices = selected_indices[:64]
        if not selected_indices:
            self._self_track_misses += 1
            if self._self_track_box is not None and time.perf_counter() - self._self_track_last_seen > 0.80:
                self._reset_self_player_track()
                self._self_track_state = 'SEARCHING'
            else:
                self._self_track_state = 'LOST' if self._self_track_box is not None else 'SEARCHING'
            return flags, scores

        now = time.perf_counter()
        ref_hist = getattr(self, '_self_id_hist_centered', None)
        ref_norms = getattr(self, '_self_id_hist_norms', None)
        ref_gray = getattr(self, '_self_id_gray_matrix', None)
        static_ready = bool(
            self._self_id_references and ref_hist is not None and ref_norms is not None
            and ref_gray is not None and len(ref_hist)
        )

        track_active = self._self_track_box is not None and self._self_track_last_seen > 0.0
        track_age = max(0.0, now - self._self_track_last_seen) if track_active else 999.0
        if track_active and track_age > 0.80:
            # Do not let a stale last-known position bias later re-identification.
            self._reset_self_player_track()
            track_active = False
            track_age = 999.0
        if track_active:
            old_cx, old_cy = self._self_track_center
            vx, vy = self._self_track_velocity
            dt = min(0.70, track_age)
            pred_x = old_cx + vx * dt
            pred_y = old_cy + vy * dt
            # Camera-flow compensation is used only when its estimate has enough support.
            if (getattr(self, '_camera_motion_affine', None) is not None
                    and getattr(self, '_camera_motion_quality', 0.0) >= 0.35
                    and getattr(self, '_camera_motion_inliers', 0) >= 8):
                try:
                    pred_x, pred_y = self._camera_transform_point(pred_x, pred_y)
                except Exception:
                    pass
            track_w, track_h = self._self_track_size
            speed = math.hypot(vx, vy)
            diagonal = math.hypot(track_w, track_h)
            gate = max(72.0, diagonal * 1.25 + min(420.0, speed * dt * 1.35))
            gate = min(gate + min(180.0, self._self_track_misses * 32.0), 0.68 * math.hypot(frame.shape[1], frame.shape[0]))
        else:
            pred_x = pred_y = 0.0
            track_w = track_h = 1.0
            gate = 0.0

        candidates = []
        for index in selected_indices:
            box = tuple(float(v) for v in boxes[index][:4])
            x1, y1, x2, y2 = box
            bw, bh = max(1.0, x2 - x1), max(1.0, y2 - y1)
            cx, cy = (x1 + x2) * 0.5, (y1 + y2) * 0.5
            descriptor = self._make_self_player_descriptor(frame, box)
            if descriptor is None:
                continue

            appearance_scores = []
            if static_ready:
                try:
                    candidate_hist = np.asarray(descriptor['hist'], dtype=np.float32).reshape(48)
                    candidate_gray = np.asarray(descriptor['gray'], dtype=np.float32).reshape(24 * 48)
                    centered = candidate_hist - float(candidate_hist.mean())
                    candidate_norm = max(1e-8, float(np.linalg.norm(centered)))
                    color_scores = np.clip(
                        (ref_hist @ centered) / (ref_norms * candidate_norm), 0.0, 1.0
                    )
                    gray_scores = np.clip(
                        1.0 - np.mean(np.abs(ref_gray - candidate_gray[None, :]), axis=1),
                        0.0, 1.0,
                    )
                    global_scores = 0.84 * color_scores + 0.16 * gray_scores
                    spatial_matrix = getattr(self, '_self_id_spatial_matrix', None)
                    spatial_valid = getattr(self, '_self_id_spatial_valid', None)
                    if (spatial_matrix is not None and spatial_valid is not None
                            and len(spatial_matrix) == len(global_scores)
                            and bool(np.any(spatial_valid))):
                        candidate_spatial = np.asarray(descriptor['spatial_hist'], dtype=np.float32).reshape(192)
                        spatial_scores = np.minimum(spatial_matrix, candidate_spatial[None, :]).sum(axis=1) / 4.0
                        combined_scores = global_scores.copy()
                        combined_scores[spatial_valid] = (
                            0.58 * global_scores[spatial_valid]
                            + 0.42 * np.clip(spatial_scores[spatial_valid], 0.0, 1.0)
                        )
                    else:
                        combined_scores = global_scores
                    appearance_scores.extend(float(v) for v in combined_scores)
                except (ValueError, TypeError, FloatingPointError):
                    pass
            for online in self._self_online_descriptors[-8:]:
                appearance_scores.append(self._compare_self_player_descriptors(descriptor, online))
            appearance = max(appearance_scores) if appearance_scores else 0.0

            if track_active:
                distance = math.hypot(cx - flow_x, cy - flow_y)
                motion_score = math.exp(-distance / max(35.0, gate * 0.52))
                size_ratio = max(1e-4, (bw * bh) / max(1.0, track_w * track_h))
                size_score = math.exp(-abs(math.log(size_ratio)) * 0.42)
                combined = 0.70 * appearance + 0.23 * motion_score + 0.07 * size_score
                inside_gate = distance <= gate
            else:
                distance = 0.0
                motion_score = 0.50
                size_score = 0.50
                combined = appearance
                inside_gate = True

            scores[index] = float(combined)
            candidates.append({
                'index': index, 'box': box, 'center': (cx, cy), 'size': (bw, bh),
                'descriptor': descriptor, 'appearance': float(appearance),
                'combined': float(combined), 'distance': float(distance),
                'motion': float(motion_score), 'size_score': float(size_score),
                'inside_gate': bool(inside_gate),
            })

        if not candidates:
            self._self_track_misses += 1
            self._self_track_state = 'LOST' if track_active else 'SEARCHING'
            if track_age > 0.80:
                self._reset_self_player_track()
                self._self_track_state = 'SEARCHING'
            return flags, scores

        candidates.sort(key=lambda item: item['combined'], reverse=True)
        best = candidates[0]

        # A sudden dash can move the avatar far outside the motion gate. Prefer
        # a uniquely stronger appearance match, but never jump to a far lookalike
        # while a similarly matching candidate remains near the old trajectory.
        dash_reacquire = False
        competing_near_lookalike = False
        if track_active:
            far_matches = [c for c in candidates if not c['inside_gate'] and c['appearance'] >= 0.94]
            near_matches = [c for c in candidates if c['inside_gate'] and c['appearance'] >= self._self_id_match_threshold - 0.08]
            if far_matches:
                far_best = max(far_matches, key=lambda item: item['appearance'])
                far_second = max(
                    (c['appearance'] for c in far_matches if c is not far_best),
                    default=0.0,
                )
                near_best_app = max((c['appearance'] for c in near_matches), default=0.0)
                if near_matches and far_best['appearance'] - near_best_app < 0.12:
                    competing_near_lookalike = True
                elif far_best['appearance'] >= 0.94 and far_best['appearance'] - far_second >= 0.10:
                    best = far_best
                    dash_reacquire = True

        remaining = [c for c in candidates if c is not best]
        second = remaining[0] if remaining else None
        margin = (best['combined'] - second['combined']) if second else 1.0
        duplicate_second = bool(
            second is not None and self._self_box_iou(best['box'], second['box']) >= 0.42
        )
        ambiguous = bool(
            competing_near_lookalike
            or (second is not None and not duplicate_second and margin < 0.045
                and best['appearance'] >= self._self_id_match_threshold - 0.05)
        )

        if track_active:
            # A current track match should be spatially plausible. A large jump
            # may still reacquire only when the appearance match is unique.
            normal_match = (
                best['inside_gate']
                and best['appearance'] >= max(0.60, self._self_id_match_threshold - 0.16)
                and best['combined'] >= self._self_id_match_threshold - 0.12
            )
            accepted = (normal_match or dash_reacquire) and not ambiguous
        else:
            accepted = (
                best['appearance'] >= self._self_id_match_threshold
                and margin >= 0.065 and not ambiguous
            )

        if not accepted:
            self._self_track_misses += 1
            if ambiguous:
                self._self_track_state = 'AMBIGUOUS'
                # Fail safe: when two non-overlapping detections are visually
                # indistinguishable and motion cannot disambiguate a dash, exclude
                # both candidates temporarily rather than risk selecting the local avatar.
                for candidate in candidates:
                    if (candidate['appearance'] >= self._self_id_match_threshold - 0.06
                            and (candidate['inside_gate'] or candidate['appearance'] >= 0.94)):
                        flags[candidate['index']] = True
            elif track_active and track_age <= 0.80:
                self._self_track_state = 'SEARCHING' if self._self_track_misses > 2 else 'VERIFYING'
            else:
                self._self_track_state = 'SEARCHING'
            self._self_track_score = float(best['combined'])
            self._self_track_last_candidate = best['index']
            return flags, scores

        # Mark the chosen detection and any overlapping duplicate boxes of the
        # same object. Never mask a second non-overlapping lookalike by appearance alone.
        matched_box = best['box']
        matched_cx, matched_cy = best['center']
        matched_bw, matched_bh = best['size']
        old_center = self._self_track_center if track_active else None
        old_time = self._self_track_last_seen if track_active else now
        elapsed = max(1.0 / 240.0, min(0.70, now - old_time)) if track_active else 0.0
        if old_center is not None and elapsed > 0.0:
            measured_vx = (matched_cx - old_center[0]) / elapsed
            measured_vy = (matched_cy - old_center[1]) / elapsed
            old_vx, old_vy = self._self_track_velocity
            new_vx = 0.52 * old_vx + 0.48 * measured_vx
            new_vy = 0.52 * old_vy + 0.48 * measured_vy
            max_speed = 4200.0
            magnitude = math.hypot(new_vx, new_vy)
            if magnitude > max_speed:
                scale = max_speed / magnitude
                new_vx *= scale
                new_vy *= scale
            self._self_track_velocity = (new_vx, new_vy)
        else:
            self._self_track_velocity = (0.0, 0.0)

        self._self_track_box = matched_box
        self._self_track_center = (matched_cx, matched_cy)
        self._self_track_size = (matched_bw, matched_bh)
        self._self_track_last_seen = now
        self._self_track_hits = min(1000, self._self_track_hits + 1)
        self._self_track_misses = 0
        self._self_track_state = 'LOCKED' if self._self_track_hits >= 2 else 'VERIFYING'
        if dash_reacquire if track_active else False:
            self._self_track_state = 'REACQUIRED'
        self._self_track_score = float(best['combined'])
        self._self_track_last_candidate = best['index']
        flags[best['index']] = True

        for candidate in candidates[1:]:
            if self._self_box_iou(matched_box, candidate['box']) >= 0.42:
                flags[candidate['index']] = True

        # Adapt the short-term appearance model only after repeated, unambiguous
        # matches. This learns new animation poses without drifting onto lookalikes.
        if (best['appearance'] >= 0.84 and not ambiguous and self._self_track_hits >= 3
                and now - self._self_track_last_adapt >= 0.22):
            self._self_online_descriptors.append(best['descriptor'])
            self._self_online_descriptors = self._self_online_descriptors[-8:]
            self._self_track_last_adapt = now
        return flags, scores

    def _load_ignore_region(self):
        """Load a normalized screen-space exclusion rectangle, if one was saved."""
        self._ignore_region_norm = None
        path = getattr(self, '_ignore_region_path', None)
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as stream:
                data = json.load(stream)
            rect = data.get('rect') if isinstance(data, dict) else None
            if isinstance(rect, dict):
                values = [rect.get('left'), rect.get('top'), rect.get('right'), rect.get('bottom')]
            elif isinstance(rect, (list, tuple)) and len(rect) == 4:
                values = list(rect)
            else:
                values = None
            if values is None:
                return
            left, top, right, bottom = (float(value) for value in values)
            left = max(0.0, min(1.0, left))
            top = max(0.0, min(1.0, top))
            right = max(0.0, min(1.0, right))
            bottom = max(0.0, min(1.0, bottom))
            if right - left < 0.002 or bottom - top < 0.002:
                return
            self._ignore_region_norm = (left, top, right, bottom)
            print(f'[INFO] Ignore region loaded: {left:.3f},{top:.3f} -> {right:.3f},{bottom:.3f}', flush=True)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f'[WARN] Could not load ignore region: {exc}', flush=True)

    def _save_ignore_region(self):
        """Persist the ignore rectangle atomically; no menu or restart is required."""
        path = getattr(self, '_ignore_region_path', None)
        if not path:
            return False
        rect = None
        if self._ignore_region_norm is not None:
            rect = [round(float(value), 6) for value in self._ignore_region_norm]
        payload = {
            'version': 1,
            'coordinate_space': 'normalized_capture_frame',
            'rect': rect,
        }
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            temporary = path + '.tmp'
            with open(temporary, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(',', ':'))
            os.replace(temporary, path)
            return True
        except OSError as exc:
            self._ignore_region_status = f'Falha ao salvar: {exc}'
            print(f'[WARN] Could not save ignore region: {exc}', flush=True)
            return False

    def _ignore_region_pixels(self, frame_shape):
        """Return the normalized ignore region in current captured-frame pixels."""
        rect = self._ignore_region_norm
        if rect is None or frame_shape is None or len(frame_shape) < 2:
            return None
        height, width = int(frame_shape[0]), int(frame_shape[1])
        if width < 2 or height < 2:
            return None
        left, top, right, bottom = rect
        x1 = max(0, min(width - 1, int(round(left * width))))
        y1 = max(0, min(height - 1, int(round(top * height))))
        x2 = max(x1 + 1, min(width, int(round(right * width))))
        y2 = max(y1 + 1, min(height, int(round(bottom * height))))
        return x1, y1, x2, y2

    def _box_in_ignore_region(self, box, frame_shape):
        """Ignore a detection as soon as any part of its box enters the region."""
        region = self._ignore_region_pixels(frame_shape)
        if region is None:
            return False
        try:
            x1, y1, x2, y2 = (float(value) for value in box[:4])
        except (TypeError, ValueError):
            return False
        rx1, ry1, rx2, ry2 = region
        # Strict positive-area intersection: no 50%/center requirement.
        overlap_w = min(x2, rx2) - max(x1, rx1)
        overlap_h = min(y2, ry2) - max(y1, ry1)
        return overlap_w > 0.0 and overlap_h > 0.0

    @staticmethod
    def _clamp_ignore_rect(rect, frame_width, frame_height, min_size=4):
        x1, y1, x2, y2 = (float(value) for value in rect)
        x1, x2 = sorted((max(0.0, min(frame_width - 1.0, x1)), max(0.0, min(frame_width - 1.0, x2))))
        y1, y2 = sorted((max(0.0, min(frame_height - 1.0, y1)), max(0.0, min(frame_height - 1.0, y2))))
        if x2 - x1 < min_size:
            x2 = min(frame_width - 1.0, x1 + min_size)
            if x2 - x1 < min_size:
                x1 = max(0.0, x2 - min_size)
        if y2 - y1 < min_size:
            y2 = min(frame_height - 1.0, y1 + min_size)
            if y2 - y1 < min_size:
                y1 = max(0.0, y2 - min_size)
        return int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))

    def _update_self_id_snapshot(self, frame, boxes, confidences, class_ids):
        """Refresh the live image used by the Numpad-3 exclusion-region editor."""
        now = time.perf_counter()
        snapshot_interval = 0.06 if self.self_id_mode else 0.35
        if now - self._self_id_snapshot_updated_at < snapshot_interval:
            return
        detections = []
        for index, box in enumerate(boxes):
            meta = self._debug_detection_meta[index] if index < len(self._debug_detection_meta) else None
            detections.append({
                'box': tuple(float(value) for value in box[:4]),
                'confidence': float(confidences[index]) if index < len(confidences) else 0.0,
                'class_id': int(class_ids[index]) if index < len(class_ids) else 0,
                'reason': str((meta or {}).get('filtered_reason') or 'CANDIDATE'),
            })
        # The capture loop creates a fresh BGR array for each frame and does
        # not modify it in-place after this point. Keep a reference instead of
        # copying the complete capture crop every snapshot interval. The editor
        # still makes its own drawing copy when it is open.
        self._self_id_snapshot_frame = frame
        self._self_id_snapshot_detections = detections
        self._self_id_snapshot_updated_at = now
        if self.self_id_mode:
            self._render_self_id_window()

    def _start_self_id_window(self):
        """Open the live Numpad-3 exclusion-region editor."""
        if self.self_id_mode:
            return
        if self._self_id_snapshot_frame is None:
            self._self_id_status = 'Aguardando o primeiro frame capturado; tente novamente em instantes.'
            print(f'[WARN] {self._self_id_status}', flush=True)
            return
        try:
            self.self_id_mode = True
            self._ignore_region_drag_mode = None
            self._ignore_region_drag_start = None
            self._ignore_region_drag_origin = None
            cv2.namedWindow(self.self_id_window_name, cv2.WINDOW_AUTOSIZE)
            cv2.setMouseCallback(self.self_id_window_name, self._on_self_id_mouse_event)
            cv2.moveWindow(self.self_id_window_name, 100, 80)
            self._render_self_id_window()
            cv2.waitKey(1)
            self.self_id_window_created = True
            self._self_id_status = 'Edite a regiao laranja. Alteracoes salvam automaticamente.'
            print('[INFO] IGNORE REGION EDITOR OPEN — Numpad 3 to close.', flush=True)
        except cv2.error as exc:
            self.self_id_mode = False
            self.self_id_window_created = False
            print(f'[ERROR] Could not open ignore-region editor: {exc}', flush=True)

    def _stop_self_id_window(self):
        if self._ignore_region_drag_mode is not None:
            self._commit_ignore_region_drag()
        self.self_id_mode = False
        if self.self_id_window_created:
            try:
                cv2.destroyWindow(self.self_id_window_name)
                cv2.waitKey(1)
            except cv2.error:
                pass
        self.self_id_window_created = False
        self._ignore_region_drag_mode = None
        self._ignore_region_drag_start = None
        self._ignore_region_drag_origin = None
        print('[INFO] IGNORE REGION EDITOR CLOSED', flush=True)

    def _render_self_id_window(self):
        if not self.self_id_mode or self._self_id_snapshot_frame is None:
            return
        try:
            image = self._self_id_snapshot_frame.copy()
            frame_h, frame_w = image.shape[:2]
            region = self._ignore_region_pixels(image.shape)
            for item in self._self_id_snapshot_detections:
                x1, y1, x2, y2 = (int(round(value)) for value in item['box'])
                ignored = self._box_in_ignore_region(item['box'], image.shape)
                color = (50, 90, 255) if ignored else (70, 210, 125)
                thickness = 2 if ignored else 1
                cv2.rectangle(image, (x1, y1), (x2, y2), color, thickness, cv2.LINE_AA)
                label = f"{item['confidence']:.2f} {'IGNORAR' if ignored else 'DETECTADO'}"
                cv2.putText(image, label, (x1, max(14, y1 - 5)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.38, color, 1, cv2.LINE_AA)

            if region is not None:
                x1, y1, x2, y2 = region
                overlay = image.copy()
                cv2.rectangle(overlay, (x1, y1), (x2, y2), (0, 105, 255), -1)
                cv2.addWeighted(overlay, 0.16, image, 0.84, 0, image)
                cv2.rectangle(image, (x1, y1), (x2, y2), (0, 145, 255), 2, cv2.LINE_AA)
                handle = 7
                for hx, hy in ((x1, y1), (x2, y1), (x1, y2), (x2, y2)):
                    cv2.rectangle(image, (hx - handle, hy - handle), (hx + handle, hy + handle),
                                  (0, 210, 255), -1, cv2.LINE_AA)
                cv2.putText(image, 'IGNORE REGION', (x1 + 5, max(16, y1 - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 175, 255), 2, cv2.LINE_AA)

            panel_h = 76
            canvas = np.full((frame_h + panel_h, frame_w, 3), (18, 22, 30), dtype=np.uint8)
            canvas[:frame_h, :frame_w] = image
            region_text = 'SEM REGIAO — arraste na imagem para criar' if region is None else (
                f'REGIAO {region[0]},{region[1]} -> {region[2]},{region[3]} px'
            )
            cv2.putText(canvas, region_text, (10, frame_h + 19),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.43, (70, 190, 255), 1, cv2.LINE_AA)
            cv2.putText(canvas, 'ARRASTE DENTRO = MOVER | CTRL + ARRASTE NA BORDA/CANTO = REDIMENSIONAR',
                        (10, frame_h + 40), cv2.FONT_HERSHEY_SIMPLEX, 0.37,
                        (235, 240, 248), 1, cv2.LINE_AA)
            cv2.putText(canvas, 'BOTAO DIREITO = LIMPAR | NUMPAD 3 ou ESC = FECHAR | SALVAMENTO AUTOMATICO',
                        (10, frame_h + 60), cv2.FONT_HERSHEY_SIMPLEX, 0.35,
                        (160, 205, 175), 1, cv2.LINE_AA)
            cv2.imshow(self.self_id_window_name, canvas)
        except cv2.error as exc:
            print(f'[WARN] Could not render ignore-region editor: {exc}', flush=True)

    def _pick_ignore_resize_sides(self, x, y, rect):
        x1, y1, x2, y2 = rect
        edge_threshold = max(12.0, min(28.0, min(x2 - x1, y2 - y1) * 0.12))
        horizontal = 'left' if abs(x - x1) <= abs(x - x2) else 'right'
        vertical = 'top' if abs(y - y1) <= abs(y - y2) else 'bottom'
        horizontal_distance = min(abs(x - x1), abs(x - x2))
        vertical_distance = min(abs(y - y1), abs(y - y2))
        sides = []
        if horizontal_distance <= edge_threshold:
            sides.append(horizontal)
        if vertical_distance <= edge_threshold:
            sides.append(vertical)
        if not sides:
            distances = {
                'left': abs(x - x1), 'right': abs(x - x2),
                'top': abs(y - y1), 'bottom': abs(y - y2),
            }
            sides = [min(distances, key=distances.get)]
        return tuple(sides)

    def _on_self_id_mouse_event(self, event, x, y, flags, param):
        """Mouse interaction: drag to create/move; CTRL+drag adjusts independent edges."""
        if not self.self_id_mode or self._self_id_snapshot_frame is None:
            return
        frame_h, frame_w = self._self_id_snapshot_frame.shape[:2]
        x = max(0, min(frame_w - 1, int(x)))
        y = max(0, min(frame_h - 1, int(y)))
        ctrl_down = bool(flags & getattr(cv2, 'EVENT_FLAG_CTRLKEY', 8))
        try:
            if hasattr(win32api, 'GetAsyncKeyState'):
                ctrl_down = ctrl_down or bool(win32api.GetAsyncKeyState(0x11) & 0x8000)
        except Exception:
            pass

        if event == cv2.EVENT_RBUTTONDOWN:
            self._ignore_region_norm = None
            self._ignore_region_drag_mode = None
            self._save_ignore_region()
            self._ignore_region_status = 'Regiao removida e salvada.'
            self._render_self_id_window()
            return

        if event == cv2.EVENT_LBUTTONDOWN:
            current = self._ignore_region_pixels(self._self_id_snapshot_frame.shape)
            self._ignore_region_drag_start = (x, y)
            self._ignore_region_drag_origin = current
            inside = current is not None and current[0] <= x <= current[2] and current[1] <= y <= current[3]
            if current is not None and inside and ctrl_down:
                self._ignore_region_drag_mode = 'resize'
                self._ignore_region_resize_sides = self._pick_ignore_resize_sides(x, y, current)
            elif current is not None and inside:
                self._ignore_region_drag_mode = 'move'
            else:
                self._ignore_region_drag_mode = 'create'
                self._ignore_region_drag_origin = None
                self._ignore_region_norm = (x / frame_w, y / frame_h, x / frame_w, y / frame_h)
            return

        if event == cv2.EVENT_MOUSEMOVE and self._ignore_region_drag_mode:
            start_x, start_y = self._ignore_region_drag_start
            if self._ignore_region_drag_mode == 'create':
                rect = self._clamp_ignore_rect((start_x, start_y, x, y), frame_w, frame_h)
            elif self._ignore_region_drag_mode == 'move':
                ox1, oy1, ox2, oy2 = self._ignore_region_drag_origin
                dx, dy = x - start_x, y - start_y
                width, height = ox2 - ox1, oy2 - oy1
                nx1 = max(0, min(frame_w - 1 - width, ox1 + dx))
                ny1 = max(0, min(frame_h - 1 - height, oy1 + dy))
                rect = (nx1, ny1, nx1 + width, ny1 + height)
            else:  # resize only the selected edge(s); opposite edges stay fixed.
                ox1, oy1, ox2, oy2 = self._ignore_region_drag_origin
                min_size = 4
                nx1, ny1, nx2, ny2 = ox1, oy1, ox2, oy2
                for side in self._ignore_region_resize_sides:
                    if side == 'left':
                        nx1 = max(0, min(ox2 - min_size, x))
                    elif side == 'right':
                        nx2 = min(frame_w - 1, max(ox1 + min_size, x))
                    elif side == 'top':
                        ny1 = max(0, min(oy2 - min_size, y))
                    elif side == 'bottom':
                        ny2 = min(frame_h - 1, max(oy1 + min_size, y))
                rect = (nx1, ny1, nx2, ny2)
            rect = self._clamp_ignore_rect(rect, frame_w, frame_h)
            self._ignore_region_norm = (
                rect[0] / frame_w, rect[1] / frame_h,
                rect[2] / frame_w, rect[3] / frame_h,
            )
            self._render_self_id_window()
            return

        if event == cv2.EVENT_LBUTTONUP and self._ignore_region_drag_mode:
            self._commit_ignore_region_drag()
            self._render_self_id_window()

    def _commit_ignore_region_drag(self):
        if self._ignore_region_norm is not None:
            if self._save_ignore_region():
                self._ignore_region_status = 'Regiao salva automaticamente.'
        else:
            self._save_ignore_region()
        self._ignore_region_drag_mode = None
        self._ignore_region_drag_start = None
        self._ignore_region_drag_origin = None
        self._ignore_region_resize_sides = ()

    def _pump_self_id_window(self):
        if not self.self_id_mode:
            return
        try:
            key = cv2.waitKey(1) & 0xFF
            if key == 27:
                self._stop_self_id_window()
                return
            visible = cv2.getWindowProperty(self.self_id_window_name, cv2.WND_PROP_VISIBLE)
            if visible < 1:
                if self._ignore_region_drag_mode is not None:
                    self._commit_ignore_region_drag()
                self.self_id_mode = False
                self.self_id_window_created = False
                self._ignore_region_drag_mode = None
        except cv2.error:
            self.self_id_mode = False
            self.self_id_window_created = False
            self._ignore_region_drag_mode = None

    def _load_primary_detector(self):
        """Load the primary model selected in the Lunar menu."""
        universal_path = os.path.join(self.lib_dir, 'best.pt')
        if self.detector_model == 'roblox':
            candidates = (
                os.path.join(self.lib_dir, 'RivalsBest.pt'),
                os.path.join(self.project_root, 'RivalsBest.pt'),
            )
            model_path = next((path for path in candidates if os.path.isfile(path)), None)
            if model_path is None:
                print(
                    '[WARN] Roblox model was selected, but RivalsBest.pt was not found; '
                    'falling back to lib\\best.pt.',
                    flush=True,
                )
                model_path = universal_path
        else:
            model_path = universal_path

        if not os.path.isfile(model_path):
            raise FileNotFoundError(f'Primary detection model not found: {model_path}')

        print(f'[INFO] Loading primary detector: {model_path}', flush=True)
        model = YOLO(model_path)
        names = getattr(model, 'names', {})
        if isinstance(names, dict):
            name_items = list(names.items())
        elif isinstance(names, (list, tuple)):
            name_items = list(enumerate(names))
        else:
            name_items = []
        target_tokens = {
            'person', 'player', 'ped', 'pedestrian', 'npc', 'npcs', 'human',
            'boneco', 'character', 'enemy', 'bot', 'dummy', 'avatar',
            'humanoid', 'mob', 'monster', 'rig', 'target', 'practice',
            'training', 'mannequin', 'soldier', 'trooper', 'guard',
        }
        self._recognized_target_ids = set()
        for class_id, name in name_items:
            normalized = str(name).strip().lower().replace('_', ' ').replace('-', ' ')
            if normalized in target_tokens or set(normalized.split()).intersection(target_tokens):
                self._recognized_target_ids.add(int(class_id))
        # Fuse Conv+BatchNorm when the installed Ultralytics build exposes it.
        # This is an inference-only graph optimization; weights and precision stay the same.
        try:
            inner_model = getattr(model, 'model', None)
            fuser = getattr(inner_model, 'fuse', None)
            if callable(fuser):
                fuser(verbose=False)
        except Exception as exc:
            print(f'[INFO] Primary model fusion skipped: {exc}', flush=True)
        self._loaded_detector_model = self.detector_model
        return model

    @staticmethod
    def _normalize_input_binding(value, fallback=None):
        if fallback is None:
            fallback = {'type': 'mouse', 'vk': 0x04}
        if isinstance(value, dict):
            kind = str(value.get('type', 'keyboard')).lower()
            try:
                vk = int(value.get('vk', fallback.get('vk', 0x04)))
            except (TypeError, ValueError):
                return dict(fallback)
            if kind == 'mouse' and vk in (0x01, 0x02, 0x04, 0x05, 0x06):
                return {'type': 'mouse', 'vk': vk}
            if kind == 'keyboard' and 1 <= vk <= 0xFE:
                return {'type': 'keyboard', 'vk': vk}
        return dict(fallback)

    def _active_fov_offset(self):
        # Visual visibility never changes the FOV's actual acquisition center.
        return float(self.aim_fov_offset_x), float(self.aim_fov_offset_y)

    def _fov_center_local(self):
        """FOV center in detector-crop coordinates, relative to the calibrated reticle."""
        offset_x, offset_y = self._active_fov_offset()
        return float(self.crosshair_x) + offset_x, float(self.crosshair_y) + offset_y

    def _fov_radius(self, frame_shape=None):
        """Return the acquisition radius; hiding the ring never disables it."""
        return max(0.0, float(self.aim_fov_size) * 0.5)

    def _aim_point_inside_fov(self, x, y, frame_shape=None):
        """Reject movement when the tracked point is outside the active circular FOV."""
        radius = self._fov_radius(frame_shape)
        if radius <= 0.0:
            return False
        center_x, center_y = self._fov_center_local()
        try:
            distance = math.hypot(float(x) - center_x, float(y) - center_y)
        except (TypeError, ValueError):
            return False
        return distance <= max(0.0, radius - 0.5)

    def _update_fov_overlay(self, detection_box):
        overlay = self.aim_fov_overlay
        if overlay is None:
            return
        visible = bool(self.aim_fov_visible and self.aim_fov_size > 0)
        if not visible or not detection_box:
            overlay.update_at_reticle(0, 0, 0, visible=False)
            overlay.pump()
            return
        center_x, center_y = self._fov_center_local()
        screen_x = float(detection_box['left']) + center_x
        screen_y = float(detection_box['top']) + center_y
        overlay.update_at_reticle(
            screen_x, screen_y, self.aim_fov_size, visible=True,
            color=self.aim_fov_color, thickness=self.aim_fov_thickness,
        )
        overlay.pump()

    def _resize_capture_for_fov(self):
        """Size the detector ROI to contain both the calibrated reticle and movable FOV."""
        active_offset_x, active_offset_y = self._active_fov_offset()
        offset_extent = max(abs(int(active_offset_x)), abs(int(active_offset_y)))
        # The acquisition circle remains active even when its ring is hidden.
        active_diameter = int(self.aim_fov_size)
        desired = max(
            int(round(self.base_box_constant * 1.40)),
            active_diameter + offset_extent + 48,
        )
        if self._max_capture_size is not None:
            desired = min(desired, int(self._max_capture_size))
        desired = max(1, desired)

        if self._capture_bounds is not None:
            left, top, right, bottom = self._capture_bounds
            new_width = max(1, min(desired, int(right - left)))
            new_height = max(1, min(desired, int(bottom - top)))
        else:
            new_width = desired
            new_height = desired

        changed = (
            desired != self.box_constant
            or new_width != self.capture_width
            or new_height != self.capture_height
        )
        if not changed:
            return False

        self.box_constant = desired
        self.capture_width = new_width
        self.capture_height = new_height
        # The crop is centered between the reticle and FOV center. This keeps
        # both visible to YOLO even when the user moves the FOV away from center.
        center_x = (new_width - 1) / 2.0
        center_y = (new_height - 1) / 2.0
        self.crosshair_x = center_x - active_offset_x * 0.5 + self._calibrated_reticle_offset_x
        self.crosshair_y = center_y - active_offset_y * 0.5 + self._calibrated_reticle_offset_y
        self._camera_prev_gray = None
        self._camera_motion_affine = None
        self._reset_aim_tracking()
        self._fov_settings_dirty = True
        print(
            f'[INFO] Detection ROI resized to {new_width}x{new_height}; '
            f'FOV={active_diameter}px offset=({self.aim_fov_offset_x},{self.aim_fov_offset_y}).',
            flush=True,
        )
        return True

    def _make_detection_box(self, primary_bounds):
        """Place the detector ROI around the reticle and the configurable FOV."""
        left, top, right, bottom = map(int, primary_bounds)
        monitor_w = max(1, right - left)
        monitor_h = max(1, bottom - top)

        reticle_screen_x = (left + right) * 0.5 + float(self._calibrated_reticle_offset_x)
        reticle_screen_y = (top + bottom) * 0.5 + float(self._calibrated_reticle_offset_y)
        active_offset_x, active_offset_y = self._active_fov_offset()
        fov_screen_x = reticle_screen_x + active_offset_x
        fov_screen_y = reticle_screen_y + active_offset_y
        crop_center_x = (reticle_screen_x + fov_screen_x) * 0.5
        crop_center_y = (reticle_screen_y + fov_screen_y) * 0.5

        width = max(1, min(int(self.capture_width), monitor_w))
        height = max(1, min(int(self.capture_height), monitor_h))
        crop_left = int(round(crop_center_x - width * 0.5))
        crop_top = int(round(crop_center_y - height * 0.5))
        crop_left = min(max(left, crop_left), right - width)
        crop_top = min(max(top, crop_top), bottom - height)

        self.capture_width = width
        self.capture_height = height
        self.crosshair_x = float(reticle_screen_x - crop_left)
        self.crosshair_y = float(reticle_screen_y - crop_top)
        box = {
            'left': int(crop_left),
            'top': int(crop_top),
            'width': int(width),
            'height': int(height),
        }
        self._current_detection_box = box
        self._reticle_screen_x = float(reticle_screen_x)
        self._reticle_screen_y = float(reticle_screen_y)
        return box

    @staticmethod
    def _dxgi_region(detection_box, primary_bounds):
        """Convert physical desktop coordinates to DXcam output-local coordinates."""
        primary_left, primary_top, primary_right, primary_bottom = map(int, primary_bounds)
        left = int(detection_box['left']) - primary_left
        top = int(detection_box['top']) - primary_top
        right = left + int(detection_box['width'])
        bottom = top + int(detection_box['height'])
        left = max(0, min(primary_right - primary_left, left))
        top = max(0, min(primary_bottom - primary_top, top))
        right = max(left + 1, min(primary_right - primary_left, right))
        bottom = max(top + 1, min(primary_bottom - primary_top, bottom))
        return (left, top, right, bottom)

    def _initialize_capture_backend(self, detection_box, primary_bounds):
        """Start a low-latency full-monitor DXGI stream with a tiny latest-frame ring.

        DXcam duplicates the complete monitor output continuously through DXGI,
        similar to desktop-sharing software. Only the existing FOV crop is copied
        from the ring buffer to the CPU for YOLO, preserving the current detector
        geometry and avoiding an extra full-screen NumPy copy per inference.
        """
        region = self._dxgi_region(detection_box, primary_bounds)
        expected_width = region[2] - region[0]
        expected_height = region[3] - region[1]
        camera = None
        try:
            import dxcam
            camera = dxcam.create(
                output_idx=0,
                output_color='BGRA',
                backend='dxgi',
                max_buffer_len=2,
            )
            # This is a continuous Desktop Duplication stream of the whole
            # primary monitor. A two-frame ring keeps latency and RAM bounded.
            camera.start(target_fps=60, video_mode=False)
            frame = None
            for _ in range(20):
                frame = camera.grab(
                    region=region,
                    copy=True,
                    new_frame_only=False,
                )
                if frame is not None and getattr(frame, 'size', 0) > 0:
                    break
                time.sleep(0.005)
            if frame is None or getattr(frame, 'size', 0) == 0:
                raise RuntimeError('DXGI nao entregou um frame inicial.')
            if frame.ndim != 3 or frame.shape[2] != 4:
                raise RuntimeError(f'Formato BGRA inesperado: {getattr(frame, "shape", None)}')
            if frame.shape[1] != expected_width or frame.shape[0] != expected_height:
                raise RuntimeError(
                    f'Regiao DXGI retornou {frame.shape[1]}x{frame.shape[0]}, '
                    f'esperado {expected_width}x{expected_height}.'
                )
            self._dxcam_camera = camera
            self._capture_first_frame = frame
            self._capture_backend_name = 'DXGI DESKTOP DUPLICATION'
            self._capture_backend_error = None
            print(
                '[INFO] Screen capture: DXGI Desktop Duplication stream ativo '
                f'a 60 FPS; monitor integral duplicado, ROI YOLO {expected_width}x{expected_height}; '
                'buffer de 2 frames.',
                flush=True,
            )
            return
        except Exception as exc:
            self._capture_backend_error = str(exc)
            try:
                if camera is not None:
                    camera.stop()
                    camera.release()
            except Exception:
                pass
            self._dxcam_camera = None
            self._mss_camera = mss.mss()
            self._capture_backend_name = 'MSS FALLBACK'
            self._capture_first_frame = self._mss_camera.grab(detection_box)
            print(
                '[WARN] DXGI indisponivel; usando MSS como fallback. '
                f'Motivo: {exc}',
                flush=True,
            )

    def _grab_screen_frame(self, detection_box, primary_bounds):
        """Return the latest BGRA detector crop from the DXGI stream or MSS."""
        if self._capture_first_frame is not None:
            frame = self._capture_first_frame
            self._capture_first_frame = None
            return frame

        if self._dxcam_camera is not None:
            region = self._dxgi_region(detection_box, primary_bounds)
            try:
                # DXcam reads the most recent desktop-duplication frame from its
                # two-slot ring; old frames are overwritten rather than queued.
                frame = self._dxcam_camera.grab(
                    region=region,
                    copy=True,
                    new_frame_only=False,
                )
                if frame is None or getattr(frame, 'size', 0) == 0:
                    raise RuntimeError('DXGI nao retornou frame valido.')
                return frame
            except Exception as exc:
                self._capture_backend_error = str(exc)
                try:
                    self._dxcam_camera.stop()
                    self._dxcam_camera.release()
                except Exception:
                    pass
                self._dxcam_camera = None
                self._mss_camera = mss.mss()
                self._capture_backend_name = 'MSS FALLBACK'
                print(
                    f'[WARN] DXGI perdeu a captura; migrando para MSS: {exc}',
                    flush=True,
                )

        if self._mss_camera is None:
            self._mss_camera = mss.mss()
            self._capture_backend_name = 'MSS FALLBACK'
        return self._mss_camera.grab(detection_box)

    def _close_capture_backend(self):
        """Release capture objects safely; safe to call more than once."""
        self._capture_first_frame = None
        if self._dxcam_camera is not None:
            try:
                self._dxcam_camera.stop()
            except Exception:
                pass
            try:
                self._dxcam_camera.release()
            except Exception:
                pass
            self._dxcam_camera = None
        if self._mss_camera is not None:
            try:
                self._mss_camera.close()
            except Exception:
                pass
            self._mss_camera = None
        if self._capture_backend_name != 'UNINITIALIZED':
            print('[INFO] Screen capture backend released.', flush=True)
        self._capture_backend_name = 'UNINITIALIZED'

    def _refresh_menu_settings(self, force=False):
        now = time.monotonic()
        if not force and now - self._menu_last_check < 0.12:
            return
        self._menu_last_check = now
        try:
            stat = os.stat(self.menu_settings_path)
            if not force and stat.st_mtime_ns == self._menu_config_mtime_ns:
                return
            with open(self.menu_settings_path, 'r', encoding='utf-8') as file:
                data = json.load(file)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f'[WARN] Could not read menu settings: {exc}')
            return

        aim_enabled = bool(data.get('aim_enabled', self.aim_enabled))
        aim_target_mode = str(data.get('aim_target_mode', self.aim_target_mode)).strip().lower()
        if aim_target_mode not in ('all', 'head'):
            aim_target_mode = 'all'
        trigger_enabled = bool(data.get('trigger_enabled', self.trigger_enabled))
        friend_filter_enabled = bool(data.get('friend_filter_enabled', self.friend_filter_enabled))
        # Ignore obsolete aim_box_* / aim_box_size keys from the old
        # rectangular Aim Assist. The circular FOV has its own setting.
        legacy_trigger_extent = max(0, min(200, int(data.get('trigger_box_size', self.trigger_box_size)))) // 2
        trigger_edges = {
            edge: max(0, min(200, int(data.get(f'trigger_box_{edge}', legacy_trigger_extent))))
            for edge in ('left', 'right', 'top', 'bottom')
        }
        trigger_box_size = max(trigger_edges['left'] + trigger_edges['right'], trigger_edges['top'] + trigger_edges['bottom'])
        aim_fov_size = max(0, min(700, int(data.get('aim_fov_size', self.aim_fov_size))))
        # The new key controls only the overlay. Fall back to the former
        # combined toggle for existing settings files on their first load.
        aim_fov_visible = bool(data.get(
            'aim_fov_visible', data.get('aim_fov_enabled', self.aim_fov_visible)
        ))
        aim_fov_thickness = max(1, min(12, int(data.get('aim_fov_thickness', self.aim_fov_thickness))))
        aim_fov_offset_x = max(-500, min(500, int(data.get('aim_fov_offset_x', self.aim_fov_offset_x))))
        aim_fov_offset_y = max(-300, min(300, int(data.get('aim_fov_offset_y', self.aim_fov_offset_y))))
        aim_hitbox_width_legacy = max(25, min(200, int(data.get('aim_hitbox_width', self.aim_hitbox_width))))
        aim_hitbox_height_legacy = max(25, min(200, int(data.get('aim_hitbox_height', self.aim_hitbox_height))))
        # Migrate a legacy symmetric size into equivalent independent edge shifts.
        legacy_left = round((100 - aim_hitbox_width_legacy) / 2)
        legacy_right = round((aim_hitbox_width_legacy - 100) / 2)
        legacy_top = round((100 - aim_hitbox_height_legacy) / 2)
        legacy_bottom = round((aim_hitbox_height_legacy - 100) / 2)
        aim_hitbox_edges = {
            'left': max(-100, min(100, int(data.get('aim_hitbox_left_offset', legacy_left)))),
            'right': max(-100, min(100, int(data.get('aim_hitbox_right_offset', legacy_right)))),
            'top': max(-100, min(100, int(data.get('aim_hitbox_top_offset', legacy_top)))),
            'bottom': max(-100, min(100, int(data.get('aim_hitbox_bottom_offset', legacy_bottom)))),
        }
        aim_hitbox_width = max(1, 100 + aim_hitbox_edges['right'] - aim_hitbox_edges['left'])
        aim_hitbox_height = max(1, 100 + aim_hitbox_edges['bottom'] - aim_hitbox_edges['top'])
        manual_input_priority = bool(data.get('manual_input_priority', self.manual_input_priority))
        aim_fov_color = str(data.get('aim_fov_color', self.aim_fov_color)).strip().upper()
        if not (len(aim_fov_color) == 7 and aim_fov_color.startswith('#')):
            aim_fov_color = self.aim_fov_color
        try:
            int(aim_fov_color[1:], 16)
        except ValueError:
            aim_fov_color = self.aim_fov_color
        # Versioned migration from the previous 0..900 / 0..200% controls.
        # Existing installations receive a strong, balanced starting point;
        # new settings use the requested 0.10..1.00 scale.
        try:
            control_scale_version = int(data.get('aim_control_scale_version', 0))
        except (TypeError, ValueError):
            control_scale_version = 0
        if control_scale_version < 1:
            aim_speed = 0.75
            aim_strength = 0.75
        else:
            try:
                raw_speed = float(data.get('aim_speed', self.aim_speed))
                aim_speed = round(max(0.10, min(1.00, raw_speed)), 2)
            except (TypeError, ValueError):
                aim_speed = float(self.aim_speed)
            try:
                raw_force = float(data.get('aim_strength', self.aim_strength))
                aim_strength = round(max(0.10, min(1.00, raw_force)), 2)
            except (TypeError, ValueError):
                aim_strength = float(self.aim_strength)
        try:
            detector_tuning_version = int(data.get('detector_tuning_version', 0))
        except (TypeError, ValueError):
            detector_tuning_version = 0
        try:
            raw_confidence = float(
                data.get('confidence', data.get('detection_confidence', self.confidence_threshold))
            )
            if (
                detector_tuning_version < 2 and abs(raw_confidence - 0.45) < 0.0001
            ) or (
                detector_tuning_version < 3 and abs(raw_confidence - 0.25) < 0.0001
            ):
                raw_confidence = 0.12
            confidence_threshold = round(max(0.08, min(0.99, raw_confidence)), 2)
        except (TypeError, ValueError):
            confidence_threshold = float(self.confidence_threshold)
        aim_method = str(data.get('aim_method', self.aim_method)).strip().lower()
        if aim_method not in ('old', 'new'):
            aim_method = 'old'
        aim_hold_lmb = bool(data.get('aim_hold_lmb', self.aim_hold_lmb))
        detector_model = str(data.get('detector_model', self.detector_model)).strip().lower()
        if detector_model not in ('universal', 'roblox'):
            detector_model = 'universal'
        trigger_delay_ms = max(0, min(1000, int(data.get('trigger_delay_ms', self.trigger_delay_ms))))
        dot_size = max(0, min(100, int(data.get('dot_size', self.dot_size))))
        dot_corner = data.get('dot_corner', self.dot_corner)
        if dot_corner not in ('top_left', 'top_right', 'bottom_left', 'bottom_right'):
            dot_corner = 'bottom_right'

        color_on = str(data.get('color_on', self.dot_color_on))
        color_off = str(data.get('color_off', self.dot_color_off))
        if not (len(color_on) == 7 and color_on.startswith('#')):
            color_on = self.dot_color_on
        if not (len(color_off) == 7 and color_off.startswith('#')):
            color_off = self.dot_color_off
        try:
            int(color_on[1:], 16)
            int(color_off[1:], 16)
        except ValueError:
            color_on, color_off = self.dot_color_on, self.dot_color_off

        old_aim_enabled = self.aim_enabled
        old_aim_target_mode = self.aim_target_mode
        old_fov_geometry = (self.aim_fov_size, self.aim_fov_offset_x, self.aim_fov_offset_y)
        old_fov_visible = self.aim_fov_visible
        old_hitbox_edges = (
            self.aim_hitbox_left_offset, self.aim_hitbox_right_offset,
            self.aim_hitbox_top_offset, self.aim_hitbox_bottom_offset,
        )
        old_aim_method = self.aim_method
        old_detector_model = self.detector_model
        old_trigger_enabled = self.trigger_enabled
        old_friend_filter_enabled = self.friend_filter_enabled
        self.aim_enabled = aim_enabled
        self.aim_target_mode = aim_target_mode
        self.trigger_enabled = trigger_enabled
        self.friend_filter_enabled = friend_filter_enabled
        self.aim_fov_size = aim_fov_size
        self.aim_fov_enabled = True  # acquisition stays active when size > 0
        self.aim_fov_visible = aim_fov_visible
        self.aim_fov_thickness = aim_fov_thickness
        self.aim_fov_color = aim_fov_color
        self.aim_fov_offset_x = aim_fov_offset_x
        self.aim_fov_offset_y = aim_fov_offset_y
        self.aim_hitbox_left_offset = aim_hitbox_edges['left']
        self.aim_hitbox_right_offset = aim_hitbox_edges['right']
        self.aim_hitbox_top_offset = aim_hitbox_edges['top']
        self.aim_hitbox_bottom_offset = aim_hitbox_edges['bottom']
        self.aim_hitbox_width = aim_hitbox_width
        self.aim_hitbox_height = aim_hitbox_height
        self.manual_input_priority = manual_input_priority
        if old_fov_geometry != (self.aim_fov_size, self.aim_fov_offset_x, self.aim_fov_offset_y):
            self._fov_settings_dirty = True
        if old_fov_visible != self.aim_fov_visible:
            self._fov_settings_dirty = True  # overlay refresh only; acquisition unchanged
        new_hitbox_edges = (
            self.aim_hitbox_left_offset, self.aim_hitbox_right_offset,
            self.aim_hitbox_top_offset, self.aim_hitbox_bottom_offset,
        )
        if old_hitbox_edges != new_hitbox_edges:
            # Re-evaluate the current detection using the new edge positions and
            # discard queued corrections computed against the previous bounds.
            self._cancel_aim_motion('HITBOX EDGES UPDATED')
            self._reset_aim_tracking()
        self.aim_speed = aim_speed
        self.aim_smooth = aim_speed  # legacy alias
        self.aim_strength = aim_strength
        self.confidence_threshold = confidence_threshold
        self.conf = confidence_threshold
        self.aim_method = aim_method
        self.aim_hold_lmb = aim_hold_lmb
        self.detector_model = detector_model
        if hasattr(self, 'model') and detector_model != self._loaded_detector_model:
            try:
                self.model = self._load_primary_detector()
            except Exception as exc:
                self.detector_model = self._loaded_detector_model or old_detector_model
                print(f'[WARN] Could not switch detector model: {exc}', flush=True)
        if old_aim_method != self.aim_method:
            self._reset_new_controller_state()
        if old_aim_target_mode != self.aim_target_mode:
            self._cancel_aim_motion('AIM TARGET MODE CHANGED')
            self._reset_aim_tracking()
        self.trigger_box_left = trigger_edges['left']
        self.trigger_box_right = trigger_edges['right']
        self.trigger_box_top = trigger_edges['top']
        self.trigger_box_bottom = trigger_edges['bottom']
        self.trigger_box_size = trigger_box_size
        self.trigger_delay_ms = trigger_delay_ms
        self.dot_size = dot_size
        self.dot_corner = dot_corner
        self.dot_color_on = color_on
        self.dot_color_off = color_off

        binding = data.get('activation_binding')
        if binding is None and 'activation_vk' in data:
            binding = {'type': 'keyboard', 'vk': int(data.get('activation_vk', 0x77))}
        normalized_binding = self._normalize_input_binding(binding, self.activation_binding)
        friend_binding = self._normalize_input_binding(
            data.get('friend_capture_binding'), self.friend_capture_binding
        )
        with self._menu_settings_lock:
            self.activation_binding = normalized_binding
            self.friend_capture_binding = friend_binding

        self._menu_config_mtime_ns = stat.st_mtime_ns
        if hasattr(self, 'status_overlay'):
            self.status_overlay.update_appearance(
                self.dot_color_on, self.dot_color_off, self.dot_corner, self.dot_size
            )
            self._update_effective_status()
        if old_trigger_enabled and not self.trigger_enabled:
            self.trigger_candidate_since = None
            if self.trigger_held:
                self.set_left_button(False)
        if old_aim_enabled and not self.aim_enabled:
            self._cancel_aim_motion('AIM DISABLED')
            self._reset_aim_tracking()
            self._aim_activation_warmup_until = 0.0
        elif not old_aim_enabled and self.aim_enabled and self.master_enabled:
            self._cancel_aim_motion('AIM ENABLED')
            self._reset_aim_tracking()
            self._aim_activation_warmup_until = 0.0
        print(
            f'[INFO] Menu config loaded: aim={self.aim_enabled}, '
            f'trigger={self.trigger_enabled}, aim_fov={self.aim_fov_size}px acquisition=ON '
            f'ring_visible={self.aim_fov_visible} offset=({self.aim_fov_offset_x},{self.aim_fov_offset_y}), '
            f'trigger_hitbox={self.trigger_box_size}, '
            f'aim_hitbox_edges=L{self.aim_hitbox_left_offset:+d} R{self.aim_hitbox_right_offset:+d} '
            f'T{self.aim_hitbox_top_offset:+d} B{self.aim_hitbox_bottom_offset:+d}, '
            f'speed={self.aim_speed:.2f}, '
            f'force={self.aim_strength:.2f}, confidence={self.confidence_threshold:.2f}, '
            f'aim_method={self.aim_method.upper()}, '
            f'w2_hold_lmb={self.aim_hold_lmb}, delay={self.trigger_delay_ms}ms, '
            f'friend_filter={self.friend_filter_enabled}'
        )

    def _update_effective_status(self):
        active = bool(self.master_enabled and (self.aim_enabled or self.trigger_enabled))
        if hasattr(self, 'status_overlay'):
            self.status_overlay.set_enabled(active)

    def _activation_watch_loop(self):
        # Edge-triggered polling with a short debounce protects MOUSE 3 from
        # switch bounce / duplicated transitions. One physical click = one toggle.
        last_binding = None
        last_down = False
        press_started_at = None
        press_accepted = False
        last_toggle_at = 0.0
        toggle_debounce_s = 0.22
        stable_press_s = 0.018
        last_friend_binding = None
        last_friend_down = False
        while not self._activation_stop.is_set():
            with self._menu_settings_lock:
                binding = dict(self.activation_binding)
                friend_binding = dict(self.friend_capture_binding)

            current_binding = (binding.get('type'), int(binding.get('vk', 0)))
            if current_binding != last_binding:
                last_binding = current_binding
                last_down = bool(win32api.GetAsyncKeyState(current_binding[1]) & 0x8000)
                press_started_at = None
                press_accepted = False
                last_toggle_at = time.monotonic()
            else:
                down = bool(win32api.GetAsyncKeyState(current_binding[1]) & 0x8000)
                now = time.monotonic()
                if down and not last_down:
                    press_started_at = now
                    press_accepted = False
                if not down:
                    press_started_at = None
                    press_accepted = False
                if (down and not press_accepted and press_started_at is not None
                        and now - press_started_at >= stable_press_s
                        and now - last_toggle_at >= toggle_debounce_s):
                    self._activation_events.put('toggle')
                    last_toggle_at = now
                    press_accepted = True
                last_down = down

            current_friend_binding = (friend_binding.get('type'), int(friend_binding.get('vk', 0)))
            if current_friend_binding != last_friend_binding:
                last_friend_binding = current_friend_binding
                last_friend_down = bool(win32api.GetAsyncKeyState(current_friend_binding[1]) & 0x8000)
            else:
                friend_down = bool(win32api.GetAsyncKeyState(current_friend_binding[1]) & 0x8000)
                if friend_down and not last_friend_down:
                    self._activation_events.put('capture_friends')
                last_friend_down = friend_down

            self._activation_stop.wait(0.004)

    def _start_activation_watcher(self):
        if self._activation_thread and self._activation_thread.is_alive():
            return
        self._activation_stop.clear()
        self._activation_thread = threading.Thread(
            target=self._activation_watch_loop,
            name='LunarFeatureToggle',
            daemon=True,
        )
        self._activation_thread.start()

    def _stop_activation_watcher(self):
        self._activation_stop.set()
        if self._activation_thread and self._activation_thread.is_alive():
            self._activation_thread.join(timeout=1.0)
        self._activation_thread = None

    def _process_activation_events(self):
        toggled = False
        while True:
            try:
                event = self._activation_events.get_nowait()
            except queue.Empty:
                break
            if event == 'toggle':
                self.master_enabled = not self.master_enabled
                toggled = True
            elif event == 'capture_friends':
                self._friend_capture_pending = True
        if toggled:
            active = bool(self.master_enabled and (self.aim_enabled or self.trigger_enabled))
            self.status_overlay.set_enabled(active)
            state = 'ENABLED' if active else 'DISABLED'
            print(colored(f'\n[!] ASSISTANCE [{state}]', 'green' if active else 'red'))
            # Discard every command from the previous activation state before
            # clearing target/controller state. The first fresh lock ramps in.
            self._cancel_aim_motion('FEATURE TOGGLE')
            self._reset_aim_tracking()
            if self.master_enabled:
                self._aim_activation_warmup_until = 0.0
            else:
                self._aim_activation_warmup_until = 0.0
                self.trigger_candidate_since = None
                if self.trigger_held:
                    self.set_left_button(False)

    def _friend_identity_crop(self, frame, box):
        """Crop the marker/nameplate band above a YOLO detection."""
        frame_h, frame_w = frame.shape[:2]
        x1, y1, x2, y2 = (int(v) for v in box)
        width = max(8, x2 - x1)
        height = max(12, y2 - y1)
        center_x = (x1 + x2) // 2
        crop_width = max(110, min(320, int(width * 1.8)))
        crop_height = max(56, min(140, int(height * 0.62)))
        left = max(0, center_x - crop_width // 2)
        right = min(frame_w, center_x + crop_width // 2)
        top = max(0, y1 - crop_height)
        # Include a very small strip inside the top of the detection box to
        # tolerate YOLO boxes that start slightly above/below the character's head.
        bottom = min(frame_h, y1 + max(2, int(height * 0.04)))
        if right <= left or bottom <= top:
            return None
        return frame[top:bottom, left:right].copy()

    def _friend_person_crop(self, frame, box):
        frame_h, frame_w = frame.shape[:2]
        x1, y1, x2, y2 = (int(v) for v in box)
        width = max(8, x2 - x1)
        height = max(12, y2 - y1)
        left = max(0, x1 - int(width * 0.18))
        right = min(frame_w, x2 + int(width * 0.18))
        top = max(0, y1 - int(height * 0.48))
        bottom = min(frame_h, y2 + int(height * 0.04))
        if right <= left or bottom <= top:
            return None
        return frame[top:bottom, left:right].copy()

    @staticmethod
    def _friend_signature_mask(crop):
        """Extract bright name glyphs and yellow/gold marker pixels for visual matching."""
        if crop is None or crop.size == 0:
            return None
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        white_text = cv2.inRange(hsv, np.array([0, 0, 172], dtype=np.uint8), np.array([180, 135, 255], dtype=np.uint8))
        yellow_marker = cv2.inRange(hsv, np.array([10, 100, 72], dtype=np.uint8), np.array([36, 255, 255], dtype=np.uint8))
        combined = cv2.bitwise_or(white_text, yellow_marker)
        kernel = np.ones((2, 2), np.uint8)
        combined = cv2.morphologyEx(combined, cv2.MORPH_CLOSE, kernel, iterations=1)
        return cv2.resize(combined, (160, 64), interpolation=cv2.INTER_AREA)

    @staticmethod
    def _has_yellow_team_marker(crop):
        """Detect a small gold/yellow downward ally marker above a character."""
        if crop is None or crop.size == 0:
            return False
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(
            hsv,
            np.array([8, 75, 65], dtype=np.uint8),
            np.array([42, 255, 255], dtype=np.uint8),
        )
        kernel = np.ones((2, 2), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        roi_h, roi_w = mask.shape[:2]
        for contour in contours:
            area = cv2.contourArea(contour)
            x, y, w, h = cv2.boundingRect(contour)
            if area < 10 or w < 4 or h < 6:
                continue
            aspect = w / float(max(1, h))
            fill = area / float(max(1, w * h))
            if not (0.35 <= aspect <= 1.45 and 0.16 <= fill <= 0.95):
                continue
            if h > roi_h * 0.82 or w > roi_w * 0.60:
                continue
            center_x = x + w / 2.0
            if abs(center_x - roi_w / 2.0) > roi_w * 0.43:
                continue
            perimeter = cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, 0.05 * perimeter, True)
            if not (3 <= len(approx) <= 9):
                continue
            # The sample marker points down: its lowest vertex should be near
            # the contour's horizontal center (allowing a little anti-aliasing).
            points = approx.reshape(-1, 2)
            bottom_y = int(points[:, 1].max())
            bottom_points = points[points[:, 1] >= bottom_y - max(1, h * 0.12)]
            if len(bottom_points) and np.any(
                np.abs(bottom_points[:, 0].astype(float) - (x + w / 2.0)) <= max(3.0, w * 0.28)
            ):
                return True
        return False

    def _load_friend_references(self, force=False):
        """Load stored nameplate/marker crops once, refreshing only when metadata changes."""
        try:
            stat = os.stat(self.friend_references_path)
            if not force and stat.st_mtime_ns == self._friend_reference_mtime_ns:
                return
            with open(self.friend_references_path, 'r', encoding='utf-8') as stream:
                metadata = json.load(stream)
            references = metadata.get('references', [])
        except FileNotFoundError:
            self._friend_reference_mtime_ns = None
            self._friend_reference_templates = []
            return
        except Exception as exc:
            print(f'[WARN] Could not load friend references: {exc}', flush=True)
            return

        templates = []
        for item in references[-32:]:
            if not isinstance(item, str):
                continue
            path = os.path.join(self.friend_dir, item)
            image = cv2.imread(path, cv2.IMREAD_COLOR)
            if image is None or image.size == 0:
                continue
            signature = self._friend_signature_mask(image)
            if signature is not None and int(np.count_nonzero(signature)) >= 8:
                templates.append(signature)
        self._friend_reference_templates = templates
        self._friend_reference_mtime_ns = stat.st_mtime_ns
        print(f'[INFO] Friend references loaded: {len(templates)}', flush=True)

    @staticmethod
    def _signature_similarity(current, reference):
        if current is None or reference is None or current.shape != reference.shape:
            return 0.0
        a = current > 0
        b = reference > 0
        best = 0.0
        # Allow small shifts because the nameplate/marker can move a few pixels
        # relative to the detected character box between frames.
        for dy in (-3, 0, 3):
            for dx in (-5, 0, 5):
                shifted = np.zeros_like(a)
                src_x1 = max(0, -dx)
                src_x2 = min(a.shape[1], a.shape[1] - dx)
                src_y1 = max(0, -dy)
                src_y2 = min(a.shape[0], a.shape[0] - dy)
                dst_x1 = max(0, dx)
                dst_x2 = dst_x1 + max(0, src_x2 - src_x1)
                dst_y1 = max(0, dy)
                dst_y2 = dst_y1 + max(0, src_y2 - src_y1)
                if dst_x2 > dst_x1 and dst_y2 > dst_y1:
                    shifted[dst_y1:dst_y2, dst_x1:dst_x2] = a[src_y1:src_y2, src_x1:src_x2]
                intersection = int(np.count_nonzero(shifted & b))
                total = int(np.count_nonzero(shifted)) + int(np.count_nonzero(b))
                if total:
                    best = max(best, (2.0 * intersection) / total)
        return best

    def _classify_friend_detections(self, frame, boxes_cpu, indices=None):
        """Classify only detections that can currently enter aim/trigger regions."""
        self._load_friend_references()
        flags = [False] * len(boxes_cpu)
        reasons = ['DEFERRED OUTSIDE AIM REGION'] * len(boxes_cpu)
        selected = range(len(boxes_cpu)) if indices is None else indices
        for index in selected:
            if index < 0 or index >= len(boxes_cpu):
                continue
            identity_crop = self._friend_identity_crop(frame, boxes_cpu[index])
            marker_match = self._has_yellow_team_marker(identity_crop)
            signature = self._friend_signature_mask(identity_crop)
            scores = [
                self._signature_similarity(signature, template)
                for template in self._friend_reference_templates
            ] if signature is not None else []
            best_score = max(scores, default=0.0)
            name_match = best_score >= 0.48
            if marker_match:
                flags[index] = True
                reasons[index] = 'ALLY MARKER'
            elif name_match:
                flags[index] = True
                reasons[index] = f'SAVED NAMEPLATE {best_score:.2f}'
            else:
                flags[index] = False
                reasons[index] = 'UNKNOWN'
        return flags, reasons

    def _capture_friend_references(self, frame, boxes_cpu):
        """Capture the current YOLO view and register every detected box as a friend sample."""
        os.makedirs(self.friend_dir, exist_ok=True)
        frame_path_tmp = self.friend_capture_frame_path + '.tmp.png'
        if cv2.imwrite(frame_path_tmp, frame):
            os.replace(frame_path_tmp, self.friend_capture_frame_path)

        refs = []
        preview_tiles = []
        captured_flags = [False] * len(boxes_cpu)
        timestamp = time.strftime('%Y%m%d_%H%M%S')
        for index, box in enumerate(boxes_cpu):
            identity_crop = self._friend_identity_crop(frame, box)
            person_crop = self._friend_person_crop(frame, box)
            if identity_crop is None or identity_crop.size == 0:
                continue
            filename = f'friend_ref_{timestamp}_{time.time_ns()}_{index}.png'
            path = os.path.join(self.friend_dir, filename)
            temp_path = path + '.tmp.png'
            if not cv2.imwrite(temp_path, identity_crop):
                continue
            os.replace(temp_path, path)
            refs.append(filename)
            captured_flags[index] = True
            if person_crop is not None and person_crop.size:
                tile = cv2.resize(person_crop, (170, 210), interpolation=cv2.INTER_AREA)
                cv2.putText(tile, 'FRIEND SAMPLE', (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (50, 225, 125), 2, cv2.LINE_AA)
                preview_tiles.append(tile)

        # Keep at most 32 references to keep startup matching inexpensive.
        try:
            with open(self.friend_references_path, 'r', encoding='utf-8') as stream:
                old_refs = json.load(stream).get('references', [])
        except Exception:
            old_refs = []
        combined = [item for item in old_refs if isinstance(item, str)] + refs
        combined = combined[-32:]
        keep = set(combined)
        for name in os.listdir(self.friend_dir):
            if name.startswith('friend_ref_') and name.endswith('.png') and name not in keep:
                try:
                    os.remove(os.path.join(self.friend_dir, name))
                except OSError:
                    pass
        metadata = {'references': combined, 'captured_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'last_capture_count': len(refs)}
        metadata_tmp = self.friend_references_path + '.tmp'
        with open(metadata_tmp, 'w', encoding='utf-8') as stream:
            json.dump(metadata, stream, indent=2)
        os.replace(metadata_tmp, self.friend_references_path)

        if preview_tiles:
            columns = min(3, len(preview_tiles))
            rows = int(math.ceil(len(preview_tiles) / float(columns)))
            canvas = np.zeros((rows * 210, columns * 170, 3), dtype=np.uint8)
            for index, tile in enumerate(preview_tiles):
                row = index // columns
                col = index % columns
                canvas[row * 210:(row + 1) * 210, col * 170:(col + 1) * 170] = tile
        else:
            canvas = cv2.resize(frame, (510, 300), interpolation=cv2.INTER_AREA)
            cv2.putText(canvas, 'NO YOLO DETECTIONS IN CAPTURE', (12, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (100, 170, 255), 2, cv2.LINE_AA)
        preview_tmp = self.friend_preview_path + '.tmp.png'
        if cv2.imwrite(preview_tmp, canvas):
            os.replace(preview_tmp, self.friend_preview_path)

        self._load_friend_references(force=True)
        print(f'[INFO] Friend visual references saved: {len(refs)} detection(s). All detections in this frame were sampled.', flush=True)
        return captured_flags

    def _rank_aim_candidate_for_reticle(self, item):
        """Rank candidates against the user's actual reticle position."""
        x1, y1, x2, y2 = map(float, item['box'])
        cx, cy = map(float, item.get('box_center', ((x1 + x2) * 0.5, (y1 + y2) * 0.5)))
        width = max(1.0, x2 - x1)
        height = max(1.0, y2 - y1)
        dx = (cx - float(self.crosshair_x)) / width
        dy = (cy - float(self.crosshair_y)) / height
        return (
            math.hypot(dx, dy),
            float(item.get('distance', float('inf'))),
            -float(item.get('confidence', 0.0)),
        )

    def _pick_initial_aim_candidate(self, candidates):
        """Prefer the resized hitbox currently under the reticle."""
        if not candidates:
            return None
        under_reticle = [
            item for item in candidates
            if str(item.get('anchor_mode', '')).upper() == 'INSIDE'
        ]
        if under_reticle:
            return min(under_reticle, key=self._rank_aim_candidate_for_reticle)
        return min(
            candidates,
            key=lambda item: (
                float(item.get('distance', float('inf'))),
                -float(item.get('confidence', 0.0)),
            ),
        )

    def _pick_manual_retarget_candidate(self, candidates, excluded_track_id=None,
                                        excluded_box=None):
        """Switch only when physical mouse movement enters another hitbox."""
        if not self._manual_mouse_active or not candidates:
            return None
        eligible = []
        for item in candidates:
            if str(item.get('anchor_mode', '')).upper() != 'INSIDE':
                continue
            if excluded_track_id is not None and item.get('track_id') == excluded_track_id:
                continue
            if excluded_box is not None:
                try:
                    if self._aim_box_iou(excluded_box, item['box']) >= 0.28:
                        continue
                except Exception:
                    pass
            eligible.append(item)
        return min(eligible, key=self._rank_aim_candidate_for_reticle) if eligible else None

    def _reset_aim_tracking(self, require_manual_retarget=False):
        self._aim_lock_wait_manual_retarget = bool(require_manual_retarget)
        self._aim_locked_box = None
        self._aim_locked_part = None
        self._aim_anchor_mode = None
        self._aim_point_u = None
        self._aim_point_v = None
        self._aim_locked_hitbox = None
        self._aim_locked_track_id = None
        self._aim_locked_class_id = None
        self._aim_lock_misses = 0
        self._aim_lock_lost_since = None
        self._aim_last_valid_measurement_time = None
        self._filtered_aim_x = None
        self._filtered_aim_y = None
        self._aim_track_center_x = None
        self._aim_track_center_y = None
        self._aim_track_velocity_x = 0.0
        self._aim_track_velocity_y = 0.0
        self._aim_track_last_time = None
        self._aim_point_velocity_x = 0.0
        self._aim_point_velocity_y = 0.0
        self._aim_point_last_time = None
        self._aim_track_state = 'IDLE'
        self._aim_first_lock_at = 0.0
        self._aim_first_lock_target_id = None
        self._aim_point_filter_x = None
        self._aim_point_filter_y = None
        self._aim_remainder_x = 0.0
        self._aim_remainder_y = 0.0
        self._aim_last_update_time = None
        self._stable_prev_error_x = None
        self._stable_prev_error_y = None
        self._stable_inside_deadzone = False
        self._adaptive_prev_error_x = None
        self._adaptive_prev_error_y = None
        self._adaptive_prev_observation_time_x = None
        self._adaptive_prev_observation_time_y = None
        self._adaptive_prev_track_id_x = None
        self._adaptive_prev_track_id_y = None
        if hasattr(self, '_adaptive_feedback_lock'):
            with self._adaptive_feedback_lock:
                self._adaptive_output_history.clear()
        self._aim_previous_error_x = None
        self._aim_previous_error_y = None
        self._aim_axis_gain_x = 1.0
        self._aim_axis_gain_y = 1.0
        self._reset_new_controller_state()

    def _reset_new_controller_state(self):
        self._new_ctrl_last_time = None
        self._new_ctrl_prev_error_x = None
        self._new_ctrl_prev_error_y = None
        self._new_ctrl_error_rate_x = 0.0
        self._new_ctrl_error_rate_y = 0.0
        self._new_ctrl_velocity_x = 0.0
        self._new_ctrl_velocity_y = 0.0
        self._new_ctrl_remainder_x = 0.0
        self._new_ctrl_remainder_y = 0.0

    @staticmethod
    def _aim_box_iou(box_a, box_b):
        ax1, ay1, ax2, ay2 = map(float, box_a)
        bx1, by1, bx2, by2 = map(float, box_b)
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
        intersection = iw * ih
        area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
        area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
        union = area_a + area_b - intersection
        return intersection / union if union > 1e-6 else 0.0

    def _try_camera_corner_phase(self, previous, current):
        """Fast global-translation check from four peripheral image patches.

        Three agreeing corners are required, so a moving object near the reticle
        cannot establish camera motion by itself. The central area is deliberately
        excluded. The returned displacement uses the downscaled frame's pixels.
        """
        frame_h, frame_w = previous.shape[:2]
        if frame_w < 96 or frame_h < 96:
            return None

        patch_w = min(frame_w - 1, max(48, int(round(frame_w * 0.36))))
        patch_h = min(frame_h - 1, max(48, int(round(frame_h * 0.36))))
        if patch_w <= 0 or patch_h <= 0:
            return None

        key = (patch_w, patch_h)
        cache = getattr(self, '_camera_phase_window_cache', None)
        if not isinstance(cache, dict):
            cache = {}
            self._camera_phase_window_cache = cache
        window = cache.get(key)
        if window is None:
            window = cv2.createHanningWindow((patch_w, patch_h), cv2.CV_32F)
            if len(cache) >= 8:
                cache.clear()
            cache[key] = window

        right = frame_w - patch_w
        bottom = frame_h - patch_h
        origins = ((0, 0), (right, 0), (0, bottom), (right, bottom))
        estimates = []
        max_shift = max(16.0, 0.22 * math.hypot(frame_w, frame_h))
        for x0, y0 in origins:
            old_patch = previous[y0:y0 + patch_h, x0:x0 + patch_w]
            new_patch = current[y0:y0 + patch_h, x0:x0 + patch_w]
            if float(np.std(old_patch)) < 3.0 or float(np.std(new_patch)) < 3.0:
                continue
            try:
                (dx, dy), response = cv2.phaseCorrelate(
                    old_patch.astype(np.float32, copy=False),
                    new_patch.astype(np.float32, copy=False),
                    window,
                )
                dx, dy, response = float(dx), float(dy), float(response)
            except (cv2.error, TypeError, ValueError, FloatingPointError):
                continue
            if not all(math.isfinite(v) for v in (dx, dy, response)):
                continue
            if response < 0.12 or math.hypot(dx, dy) > max_shift:
                continue
            estimates.append((dx, dy, max(0.0, min(1.0, response))))

        if len(estimates) < 3:
            return None
        shifts = np.asarray([(item[0], item[1]) for item in estimates], dtype=np.float32)
        median_shift = np.median(shifts, axis=0)
        residuals = np.linalg.norm(shifts - median_shift.reshape(1, 2), axis=1)
        inlier_mask = residuals <= 1.75
        inlier_count = int(np.count_nonzero(inlier_mask))
        if inlier_count < 3:
            return None
        inlier_shifts = shifts[inlier_mask]
        final_shift = np.median(inlier_shifts, axis=0)
        final_residuals = np.linalg.norm(
            inlier_shifts - final_shift.reshape(1, 2), axis=1
        )
        responses = np.asarray(
            [estimates[i][2] for i in range(len(estimates)) if inlier_mask[i]],
            dtype=np.float32,
        )
        median_response = float(np.median(responses)) if len(responses) else 0.0
        median_residual = float(np.median(final_residuals)) if len(final_residuals) else 999.0
        if median_response < 0.22 or median_residual > 1.25:
            return None
        return (
            float(final_shift[0]), float(final_shift[1]),
            median_response, inlier_count, inlier_count / 4.0, median_residual,
        )

    def _update_camera_motion(self, frame, frame_timestamp=None):
        """Run global camera flow at a bounded cadence to reduce CPU contention.

        The full optical-flow computation and its geometric validation are
        unchanged. Skipped frames do not reuse an old affine transform, avoiding
        repeated application of the same camera shift.
        """
        now = time.perf_counter()
        interval = max(1.0 / 60.0, float(self._camera_motion_min_interval))
        if self._camera_motion_last_update_at and now - self._camera_motion_last_update_at < interval:
            self._camera_motion_affine = None
            self._camera_motion_dx = 0.0
            self._camera_motion_dy = 0.0
            self._camera_motion_quality = 0.0
            self._camera_motion_inliers = 0
            self._camera_motion_cells = 0
            self._camera_motion_reject_reason = 'CADENCE SKIP'
            self._camera_motion_model = 'NONE'
            self._camera_motion_coverage = 0.0
            self._camera_motion_residual = 0.0
            self._last_camera_flow_ms = 0.0
            return

        self._camera_motion_last_update_at = now
        started = time.perf_counter()
        try:
            self._update_camera_motion_full(frame, frame_timestamp=frame_timestamp)
        finally:
            self._last_camera_flow_ms = (time.perf_counter() - started) * 1000.0

    def _update_camera_motion_full(self, frame, frame_timestamp=None):
        """Estimate camera motion from spatially distributed scene features.

        Features are sampled independently from a 3x3 grid. A moving character
        or effect can contaminate one or two cells without dominating the global
        estimate. Translation is accepted from consistent cell medians; rotation
        and small affine motion use the same tracked points as a geometric fallback.
        """
        self._camera_motion_affine = None
        self._camera_motion_dx = 0.0
        self._camera_motion_dy = 0.0
        self._camera_motion_quality = 0.0
        self._camera_motion_inliers = 0
        self._camera_motion_coverage = 0.0
        self._camera_motion_residual = 0.0
        self._camera_motion_cells = 0
        self._camera_motion_reject_reason = 'NO FLOW'
        self._camera_motion_model = 'NONE'
        try:
            height, width = frame.shape[:2]
            if height < 48 or width < 48:
                self._camera_prev_gray = None
                self._camera_motion_reject_reason = 'FRAME SMALL'
                return

            scale = min(1.0, 256.0 / float(max(height, width)))
            small_size = (max(32, int(round(width * scale))),
                          max(32, int(round(height * scale))))
            if small_size != (width, height):
                small_bgr = cv2.resize(frame, small_size, interpolation=cv2.INTER_AREA)
            else:
                small_bgr = frame
            # Resize before BGR->gray conversion: the previous path converted the
            # entire detector crop to gray and only then reduced it to 256px.
            gray = cv2.cvtColor(small_bgr, cv2.COLOR_BGR2GRAY)

            previous = self._camera_prev_gray
            self._camera_prev_gray = gray
            if previous is None or previous.shape != gray.shape:
                self._camera_motion_reject_reason = 'FIRST FRAME'
                self._camera_motion_model = 'NONE'
                return

            # Most ordinary camera pans are a global translation. Check four
            # separate corners first; this avoids the nine-tile feature search and
            # RANSAC in the common case. Fall back to the geometric tracker for
            # rotation, weak texture, or disagreement between corners.
            phase_result = self._try_camera_corner_phase(previous, gray)
            if phase_result is not None:
                phase_dx, phase_dy, phase_response, phase_tiles, phase_coverage, phase_residual = phase_result
                full_dx = phase_dx / scale
                full_dy = phase_dy / scale
                translation = math.hypot(full_dx, full_dy)
                max_translation = max(52.0, 0.18 * math.hypot(width, height))
                if translation <= max_translation:
                    self._camera_motion_affine = np.asarray(
                        [[1.0, 0.0, full_dx], [0.0, 1.0, full_dy]],
                        dtype=np.float64,
                    )
                    self._camera_motion_dx = float(full_dx)
                    self._camera_motion_dy = float(full_dy)
                    self._camera_motion_quality = float(phase_response)
                    self._camera_motion_inliers = int(phase_tiles)
                    self._camera_motion_cells = int(phase_tiles)
                    self._camera_motion_coverage = float(phase_coverage)
                    self._camera_motion_residual = float(phase_residual)
                    self._camera_motion_model = 'CORNER PHASE'
                    self._camera_motion_reject_reason = 'CORNER CONSENSUS'
                    self._record_camera_flow_sample(frame_timestamp)
                    return
                self._camera_motion_reject_reason = 'PHASE SHIFT OUTLIER'

            frame_h, frame_w = gray.shape[:2]
            grid_rows = 3
            grid_cols = 3
            point_groups = []
            point_cell_ids = []
            total_features = 0
            for row in range(grid_rows):
                y0 = int(round(row * frame_h / grid_rows))
                y1 = int(round((row + 1) * frame_h / grid_rows))
                for col in range(grid_cols):
                    x0 = int(round(col * frame_w / grid_cols))
                    x1 = int(round((col + 1) * frame_w / grid_cols))
                    tile = previous[y0:y1, x0:x1]
                    if tile.size == 0 or min(tile.shape[:2]) < 20:
                        continue
                    points = cv2.goodFeaturesToTrack(
                        tile, maxCorners=28, qualityLevel=0.010,
                        minDistance=5, blockSize=5, useHarrisDetector=False,
                    )
                    if points is None or len(points) < 4:
                        continue
                    points = points.reshape(-1, 2).astype(np.float32)
                    points[:, 0] += float(x0)
                    points[:, 1] += float(y0)
                    point_groups.append(points)
                    point_cell_ids.extend([row * grid_cols + col] * len(points))
                    total_features += len(points)

            if total_features < 18:
                self._camera_motion_reject_reason = 'FEW GRID FEATURES'
                return

            previous_points = np.concatenate(point_groups, axis=0).reshape(-1, 1, 2)
            cell_ids = np.asarray(point_cell_ids, dtype=np.int32)
            current_points, status, errors = cv2.calcOpticalFlowPyrLK(
                previous, gray, previous_points, None,
                winSize=(21, 21), maxLevel=3,
                criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 24, 0.015),
            )
            if current_points is None or status is None:
                self._camera_motion_reject_reason = 'FLOW FAILED'
                return

            good = status.reshape(-1).astype(bool)
            if errors is not None:
                good &= errors.reshape(-1) < 22.0
            good &= np.isfinite(current_points.reshape(-1, 2)).all(axis=1)
            old_all = previous_points.reshape(-1, 2)
            new_all = current_points.reshape(-1, 2)
            old_pts = old_all[good]
            new_pts = new_all[good]
            good_cells = cell_ids[good]
            if len(old_pts) < 16:
                self._camera_motion_reject_reason = 'FEW TRACKS'
                return

            cell_vectors = []
            cell_support = {}
            support_rows = set()
            support_cols = set()
            for cell_id in range(grid_rows * grid_cols):
                idx = np.flatnonzero(good_cells == cell_id)
                if len(idx) < 4:
                    continue
                displacement = new_pts[idx] - old_pts[idx]
                median_vector = np.median(displacement, axis=0)
                local_residual = np.linalg.norm(displacement - median_vector[None, :], axis=1)
                if float(np.median(local_residual)) > 2.2 or float(np.percentile(local_residual, 85)) > 4.2:
                    continue
                cell_vectors.append((cell_id, float(median_vector[0]), float(median_vector[1]), len(idx)))
                cell_support[cell_id] = idx
                support_rows.add(cell_id // grid_cols)
                support_cols.add(cell_id % grid_cols)

            self._camera_motion_cells = len(cell_vectors)
            supported_points = sum(item[3] for item in cell_vectors)
            coverage = len(cell_vectors) / float(grid_rows * grid_cols)
            self._camera_motion_coverage = coverage
            self._camera_motion_quality = supported_points / float(max(1, len(old_pts)))

            if len(cell_vectors) >= 4 and len(support_rows) >= 2 and len(support_cols) >= 2:
                cell_dx = np.asarray([item[1] for item in cell_vectors], dtype=np.float64)
                cell_dy = np.asarray([item[2] for item in cell_vectors], dtype=np.float64)
                dx_small = float(np.median(cell_dx))
                dy_small = float(np.median(cell_dy))
                tile_residual = np.hypot(cell_dx - dx_small, cell_dy - dy_small)
                median_tile_residual = float(np.median(tile_residual))
                p90_tile_residual = float(np.percentile(tile_residual, 90))

                if median_tile_residual <= 1.35 and p90_tile_residual <= 2.8:
                    dx = dx_small / scale
                    dy = dy_small / scale
                    translation = math.hypot(dx, dy)
                    max_translation = max(52.0, 0.18 * math.hypot(width, height))
                    if translation <= max_translation:
                        affine = np.asarray([[1.0, 0.0, dx], [0.0, 1.0, dy]], dtype=np.float64)
                        self._camera_motion_affine = affine
                        self._camera_motion_dx = float(dx)
                        self._camera_motion_dy = float(dy)
                        self._camera_motion_inliers = int(supported_points)
                        self._camera_motion_residual = median_tile_residual
                        self._camera_motion_model = 'GRID TRANSLATION'
                        self._camera_motion_reject_reason = 'GRID TRANSLATION'
                        self._record_camera_flow_sample(frame_timestamp)
                        return

            # Affine fallback reuses the grid's distributed LK points. Inlier
            # support must span several cells, not merely form a dense local patch.
            affine, inlier_mask = cv2.estimateAffinePartial2D(
                old_pts, new_pts, method=cv2.RANSAC,
                ransacReprojThreshold=2.4, maxIters=240,
                confidence=0.995, refineIters=10,
            )
            if affine is None or inlier_mask is None:
                self._camera_motion_reject_reason = 'NO GLOBAL CONSENSUS'
                return
            inlier_mask = inlier_mask.reshape(-1).astype(bool)
            inlier_old = old_pts[inlier_mask]
            inlier_new = new_pts[inlier_mask]
            inlier_cells = good_cells[inlier_mask]
            inliers = int(len(inlier_old))
            quality = inliers / float(max(1, len(old_pts)))
            unique_cells = set(int(v) for v in inlier_cells)
            inlier_rows = {cell // grid_cols for cell in unique_cells}
            inlier_cols = {cell % grid_cols for cell in unique_cells}
            self._camera_motion_inliers = inliers
            self._camera_motion_quality = float(quality)
            self._camera_motion_cells = len(unique_cells)
            self._camera_motion_coverage = len(unique_cells) / float(grid_rows * grid_cols)
            if inliers < 16 or quality < 0.52 or len(unique_cells) < 4 or len(inlier_rows) < 2 or len(inlier_cols) < 2:
                self._camera_motion_reject_reason = 'LOW SPATIAL SUPPORT'
                return

            span_x = float(np.ptp(inlier_old[:, 0])) / max(1.0, float(frame_w))
            span_y = float(np.ptp(inlier_old[:, 1])) / max(1.0, float(frame_h))
            hull = cv2.convexHull(inlier_old.astype(np.float32))
            hull_area = float(cv2.contourArea(hull))
            hull_coverage = hull_area / max(1.0, float(frame_w * frame_h))
            if span_x < 0.28 or span_y < 0.22 or hull_coverage < 0.020:
                self._camera_motion_reject_reason = 'LOCAL FEATURES'
                return

            affine = np.asarray(affine, dtype=np.float64)
            projected = inlier_old @ affine[:, :2].T + affine[:, 2]
            residuals = np.linalg.norm(projected - inlier_new, axis=1)
            median_residual = float(np.median(residuals))
            p90_residual = float(np.percentile(residuals, 90))
            self._camera_motion_residual = median_residual
            if median_residual > 1.55 or p90_residual > 3.2:
                self._camera_motion_reject_reason = 'HIGH RESIDUAL'
                return

            affine[0, 2] /= scale
            affine[1, 2] /= scale
            scale_x = math.hypot(float(affine[0, 0]), float(affine[1, 0]))
            scale_y = math.hypot(float(affine[0, 1]), float(affine[1, 1]))
            translation = math.hypot(float(affine[0, 2]), float(affine[1, 2]))
            rotation = abs(math.atan2(float(affine[1, 0]), float(affine[0, 0])))
            max_translation = max(52.0, 0.18 * math.hypot(width, height))
            if not (0.94 <= scale_x <= 1.06 and 0.94 <= scale_y <= 1.06):
                self._camera_motion_reject_reason = 'SCALE OUTLIER'
                return
            if rotation > 0.13 or translation > max_translation:
                self._camera_motion_reject_reason = 'MOTION OUTLIER'
                return

            self._camera_motion_affine = affine
            self._camera_motion_dx = float(affine[0, 2])
            self._camera_motion_dy = float(affine[1, 2])
            self._camera_motion_model = 'GRID AFFINE'
            self._camera_motion_reject_reason = 'GRID AFFINE'
            self._record_camera_flow_sample(frame_timestamp)
        except (cv2.error, ValueError, TypeError, FloatingPointError, OverflowError) as exc:
            self._camera_motion_affine = None
            self._camera_motion_reject_reason = f'FLOW ERROR: {type(exc).__name__}'

    def _record_camera_flow_sample(self, frame_timestamp=None):
        """Store global flow at the timestamp of the frame being measured."""
        flow_stamp = time.perf_counter() if frame_timestamp is None else float(frame_timestamp)
        with self._adaptive_feedback_lock:
            self._camera_flow_history.append((
                flow_stamp,
                float(self._camera_motion_dx),
                float(self._camera_motion_dy),
                float(self._camera_motion_quality),
                int(self._camera_motion_inliers),
                float(self._camera_motion_coverage),
                float(self._camera_motion_residual),
            ))
            flow_cutoff = flow_stamp - 0.65
            if len(self._camera_flow_history) > 256:
                self._camera_flow_history = [
                    sample for sample in self._camera_flow_history if sample[0] >= flow_cutoff
                ][-192:]

    def _camera_transform_point(self, x, y):
        affine = getattr(self, '_camera_motion_affine', None)
        if affine is None:
            return float(x), float(y)
        x = float(x)
        y = float(y)
        return (
            float(affine[0, 0] * x + affine[0, 1] * y + affine[0, 2]),
            float(affine[1, 0] * x + affine[1, 1] * y + affine[1, 2]),
        )

    def _camera_transform_box(self, box):
        x1, y1, x2, y2 = map(float, box)
        corners = (
            self._camera_transform_point(x1, y1),
            self._camera_transform_point(x2, y1),
            self._camera_transform_point(x1, y2),
            self._camera_transform_point(x2, y2),
        )
        xs = [point[0] for point in corners]
        ys = [point[1] for point in corners]
        return (min(xs), min(ys), max(xs), max(ys))

    def _compensate_aim_point_for_camera_motion(self):
        """Move the prior filtered hitbox anchor into the current camera frame.

        Doing this before the 1-euro filter removes the large common screen shift
        caused by manual camera movement. The filter then smooths only residual
        target motion/detection noise, rather than lagging behind every pan.
        """
        affine = getattr(self, '_camera_motion_affine', None)
        if affine is None:
            return
        if self._filtered_aim_x is not None and self._filtered_aim_y is not None:
            self._filtered_aim_x, self._filtered_aim_y = self._camera_transform_point(
                self._filtered_aim_x, self._filtered_aim_y
            )

        state_x = getattr(self, '_aim_point_filter_x', None)
        state_y = getattr(self, '_aim_point_filter_y', None)
        if not isinstance(state_x, dict) or not isinstance(state_y, dict):
            return
        for field in ('raw', 'filtered'):
            px, py = self._camera_transform_point(state_x[field], state_y[field])
            state_x[field], state_y[field] = px, py
        dx = float(state_x.get('derivative', 0.0))
        dy = float(state_y.get('derivative', 0.0))
        state_x['derivative'] = float(affine[0, 0] * dx + affine[0, 1] * dy)
        state_y['derivative'] = float(affine[1, 0] * dx + affine[1, 1] * dy)
        self._aim_point_velocity_x = state_x['derivative']
        self._aim_point_velocity_y = state_y['derivative']

    @staticmethod
    def _filter_alpha(cutoff_hz, dt):
        cutoff_hz = max(0.01, float(cutoff_hz))
        dt = max(1.0 / 1000.0, float(dt))
        tau = 1.0 / (2.0 * math.pi * cutoff_hz)
        return 1.0 / (1.0 + tau / dt)

    def _one_euro_point(self, axis, measurement, now, reset=False):
        """Filter current detector measurements; open bandwidth on abrupt real motion.

        Fast motion is taken from the newest observed box. No future position is
        generated here: the output always remains between prior filtered state
        and the current detector measurement.
        """
        attr = '_aim_point_filter_' + axis
        state = getattr(self, attr, None)
        measurement = float(measurement)
        if reset or state is None:
            state = {
                'raw': measurement,
                'filtered': measurement,
                'derivative': 0.0,
                'time': float(now),
            }
            setattr(self, attr, state)
            return measurement, 0.0

        dt = max(1.0 / 240.0, min(0.12, float(now) - float(state['time'])))
        raw_derivative = (measurement - float(state['raw'])) / dt
        raw_derivative = max(-3000.0, min(3000.0, raw_derivative))
        raw_speed = abs(raw_derivative)

        # Open the filter bandwidth earlier on measured movement. The previous
        # low derivative cutoff held moderate movement for several frames, which
        # made the marker visibly trail a moving character. This remains a
        # measurement-only filter: it never projects a future position.
        derivative_cutoff = (
            10.0 if raw_speed < 65.0
            else min(70.0, 10.0 + raw_speed * 0.040)
        )
        derivative_alpha = self._filter_alpha(derivative_cutoff, dt)
        derivative = (
            derivative_alpha * raw_derivative
            + (1.0 - derivative_alpha) * float(state['derivative'])
        )

        # Let a large, newly measured displacement influence the filter now,
        # while retaining a small stationary deadband for detector jitter.
        responsive_speed = max(abs(derivative), min(raw_speed, 2600.0) * 0.84)
        # A higher minimum bandwidth prevents slow strafes from trailing the
        # detector box. The sub-pixel stationary deadband above still rejects
        # tiny detector jitter while the target is not moving.
        cutoff = max(18.0, min(140.0, 18.0 + 0.30 * responsive_speed))
        value_alpha = self._filter_alpha(cutoff, dt)
        previous = float(state['filtered'])
        delta = measurement - previous

        if abs(delta) < 0.50 and responsive_speed < 22.0:
            filtered = previous
        else:
            filtered = previous + value_alpha * delta

        state.update({
            'raw': measurement,
            'filtered': filtered,
            'derivative': derivative,
            'time': float(now),
        })
        setattr(self, attr, state)
        return filtered, derivative

    def _update_detector_budget(self, elapsed_ms):
        """Choose YOLO input size from measured latency, with fast downshift.

        512px is the default performance point. 640px is used only when the
        base inference has sustained headroom; 768px is reserved for unusually
        fast systems. If 512px itself misses budget, 448px is the safe fallback.
        """
        try:
            elapsed_ms = float(elapsed_ms)
        except (TypeError, ValueError):
            return
        if not math.isfinite(elapsed_ms) or elapsed_ms <= 0.0:
            return

        previous = float(getattr(self, '_detector_latency_ema_ms', 0.0) or 0.0)
        ema = elapsed_ms if previous <= 0.0 else 0.78 * previous + 0.22 * elapsed_ms
        self._detector_latency_ema_ms = ema
        current_size = int(getattr(self, '_detector_imgsz', 512))

        slow_limit = {768: 25.0, 640: 29.0, 512: 44.0}.get(current_size, 44.0)
        fast_limit = {768: 15.0, 640: 17.5, 512: 23.0, 448: 36.0}.get(current_size, 15.0)
        self._detector_slow_streak = (
            int(getattr(self, '_detector_slow_streak', 0)) + 1 if ema > slow_limit else 0
        )
        self._detector_fast_streak = (
            int(getattr(self, '_detector_fast_streak', 0)) + 1 if ema < fast_limit else 0
        )

        new_size = current_size
        if current_size >= 768 and self._detector_slow_streak >= 3:
            new_size = 640
        elif current_size >= 640 and self._detector_slow_streak >= 3:
            new_size = 512
        elif current_size <= 512 and self._detector_slow_streak >= 3 and ema > 58.0:
            new_size = 448
        elif current_size <= 448 and self._detector_fast_streak >= 45 and ema < 39.0:
            new_size = 512
        elif current_size <= 512 and self._detector_fast_streak >= 45:
            new_size = 640
        elif current_size <= 640 and self._detector_fast_streak >= 120:
            new_size = 768

        if new_size != current_size:
            self._detector_imgsz = new_size
            self._detector_slow_streak = 0
            self._detector_fast_streak = 0
            print(
                f'[PERF] YOLO input changed {current_size}px -> {new_size}px '
                f'(base inference EMA {ema:.1f}ms).',
                flush=True,
            )

    def _run_detector_pass(self, image, confidence_floor=0.045):
        """Run one bounded YOLO pass; retry only smaller sizes after allocation errors."""
        last_error = None
        requested = int(getattr(self, '_detector_imgsz', 512))
        attempts = [requested]
        for candidate in (640, 576, 512, 448, 416):
            if candidate < requested and candidate not in attempts:
                attempts.append(candidate)

        for inference_size in attempts:
            pass_started = time.perf_counter()
            try:
                prediction = self.model.predict(
                    source=image,
                    verbose=False,
                    conf=float(confidence_floor),
                    iou=self.iou,
                    imgsz=inference_size,
                    max_det=64,
                )[0]
                boxes_obj = getattr(prediction, 'boxes', None)
                boxes_data = getattr(boxes_obj, 'data', None)
                if boxes_data is None or len(boxes_data) == 0:
                    rows = np.empty((0, 6), dtype=np.float32)
                else:
                    if hasattr(boxes_data, 'detach'):
                        rows = boxes_data.detach().cpu().numpy()
                    else:
                        rows = np.asarray(boxes_data)
                    rows = np.asarray(rows, dtype=np.float32)
                    if rows.ndim != 2 or rows.shape[1] < 4:
                        rows = np.empty((0, 6), dtype=np.float32)
                    elif rows.shape[1] == 4:
                        rows = np.column_stack((rows, np.ones(len(rows)), np.zeros(len(rows))))
                    elif rows.shape[1] == 5:
                        rows = np.column_stack((rows, np.zeros(len(rows))))
                    else:
                        rows = rows[:, :6]
                self._last_detector_pass_ms = (time.perf_counter() - pass_started) * 1000.0
                if inference_size < requested:
                    self._detector_imgsz = inference_size
                    if not self._detector_size_fallback_logged:
                        print(
                            f'[WARN] YOLO input fell back from {requested}px to {inference_size}px after an allocation/runtime error.',
                            flush=True,
                        )
                        self._detector_size_fallback_logged = True
                return rows, inference_size
            except Exception as exc:
                self._last_detector_pass_ms = (time.perf_counter() - pass_started) * 1000.0
                last_error = exc
                is_oom = ('out of memory' in str(exc).lower() or 'cuda error' in str(exc).lower())
                if torch.cuda.is_available() and is_oom:
                    try:
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
                if not self._detector_size_fallback_logged:
                    print(
                        f'[WARN] YOLO {inference_size}px pass failed; trying a smaller size: {exc}',
                        flush=True,
                    )
                    self._detector_size_fallback_logged = True
                if not is_oom and inference_size != requested:
                    break
        raise RuntimeError(f'Detector failed at all input sizes {attempts}: {last_error}')

    def _should_run_zoom_pass(self, rows, frame_shape):
        """Use optional ROI enlargement only when the base pass misses the FOV."""
        if rows is None or len(rows) == 0:
            self._last_zoom_reason = 'no base detections'
            return True

        radius = self._fov_radius(frame_shape)
        fov_cx, fov_cy = self._fov_center_local()
        # Use exactly the same acceptance threshold as the later record filter.
        # The old 7% test marked 8-13% boxes as usable here, then discarded them
        # at 14% below; that prevented the ROI zoom from refining weak enemies.
        effective_threshold = max(
            float(self._target_confidence_floor),
            min(float(self.conf), 0.14),
        )
        any_usable_in_fov = False
        small_or_weak_in_fov = False
        for row in rows:
            x1, y1, x2, y2 = map(float, row[:4])
            confidence_value = float(row[4]) if len(row) > 4 else 0.0
            nearest_x = min(max(fov_cx, x1), x2)
            nearest_y = min(max(fov_cy, y1), y2)
            edge_distance = math.hypot(nearest_x - fov_cx, nearest_y - fov_cy)
            if edge_distance > radius + 12.0:
                continue
            if confidence_value >= effective_threshold:
                any_usable_in_fov = True
            else:
                small_or_weak_in_fov = True

        if any_usable_in_fov:
            self._last_zoom_reason = 'base pass sees target in FOV'
            return False
        self._last_zoom_reason = 'weak box in FOV' if small_or_weak_in_fov else 'no box near FOV'
        return True

    def _merge_multiscale_rows(self, base_rows, zoom_rows, zoom_factor, zoom_offset=(0, 0)):
        """Map zoom-ROI boxes back to the source frame and suppress duplicates."""
        candidates = []
        if base_rows is not None and len(base_rows):
            candidates.extend(np.asarray(base_rows, dtype=np.float32).tolist())
        if zoom_rows is not None and len(zoom_rows):
            mapped = np.asarray(zoom_rows, dtype=np.float32).copy()
            mapped[:, [0, 2]] = mapped[:, [0, 2]] / float(zoom_factor) + float(zoom_offset[0])
            mapped[:, [1, 3]] = mapped[:, [1, 3]] / float(zoom_factor) + float(zoom_offset[1])
            candidates.extend(mapped.tolist())
        if not candidates:
            return np.empty((0, 6), dtype=np.float32)

        candidates.sort(key=lambda row: float(row[4]), reverse=True)
        kept = []
        for row in candidates:
            box = tuple(map(float, row[:4]))
            row_class = int(row[5])
            duplicate = False
            for existing in kept:
                if int(existing[5]) != row_class:
                    continue
                other = tuple(map(float, existing[:4]))
                iou = self._aim_box_iou(box, other)
                ix1, iy1 = max(box[0], other[0]), max(box[1], other[1])
                ix2, iy2 = min(box[2], other[2]), min(box[3], other[3])
                intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
                area_a = max(1.0, (box[2] - box[0]) * (box[3] - box[1]))
                area_b = max(1.0, (other[2] - other[0]) * (other[3] - other[1]))
                containment = intersection / min(area_a, area_b)
                center_a = ((box[0] + box[2]) * 0.5, (box[1] + box[3]) * 0.5)
                center_b = ((other[0] + other[2]) * 0.5, (other[1] + other[3]) * 0.5)
                center_distance = math.hypot(center_a[0] - center_b[0], center_a[1] - center_b[1])
                diagonal = max(
                    math.hypot(box[2] - box[0], box[3] - box[1]),
                    math.hypot(other[2] - other[0], other[3] - other[1]),
                )
                # Base/zoom duplicates have nearly the same center. A broad
                # IoU-only rule erased distinct same-class characters when
                # their hitboxes overlapped during close-range movement.
                duplicate = (
                    center_distance <= max(5.0, min(9.0, diagonal * 0.08))
                    and (iou >= 0.40 or containment >= 0.70)
                )
                if duplicate:
                    duplicate = True
                    break
            if not duplicate:
                kept.append(row)
        return np.asarray(kept, dtype=np.float32).reshape(-1, 6)

    def _make_zoom_roi(self, frame, base_rows):
        """Crop only the FOV/uncertain target before optional enlargement.

        The crop always contains the reticle and the selected weak/small box.
        This avoids allocating and resizing a full-frame 1.8x image when the
        useful search area is smaller, while preserving the same YOLO input size.
        """
        frame_h, frame_w = frame.shape[:2]
        if frame_h <= 0 or frame_w <= 0:
            return frame, (0, 0)

        fov_cx, fov_cy = self._fov_center_local()
        cx = min(max(float(fov_cx), 0.0), frame_w - 1.0)
        cy = min(max(float(fov_cy), 0.0), frame_h - 1.0)
        radius = self._fov_radius(frame.shape)
        selected = None
        selected_distance = float('inf')
        if base_rows is not None and len(base_rows):
            for row in base_rows:
                x1, y1, x2, y2 = map(float, row[:4])
                nearest_x = min(max(cx, x1), x2)
                nearest_y = min(max(cy, y1), y2)
                distance = math.hypot(nearest_x - cx, nearest_y - cy)
                if distance <= radius + 24.0 and distance < selected_distance:
                    selected = (x1, y1, x2, y2)
                    selected_distance = distance

        if selected is not None:
            x1, y1, x2, y2 = selected
            target_cx = (x1 + x2) * 0.5
            target_cy = (y1 + y2) * 0.5
            box_w = max(1.0, x2 - x1)
            box_h = max(1.0, y2 - y1)
            center_x = (cx + target_cx) * 0.5
            center_y = (cy + target_cy) * 0.5
            required_w = 2.0 * abs(target_cx - cx) + box_w * 1.55 + 48.0
            required_h = 2.0 * abs(target_cy - cy) + box_h * 1.55 + 48.0
        else:
            center_x, center_y = cx, cy
            required_w = max(224.0, float(self.aim_fov_size) * 1.65 + 48.0)
            required_h = max(224.0, float(self.aim_fov_size) * 1.65 + 48.0)

        crop_w = int(min(frame_w, max(224.0, required_w, float(self.aim_fov_size) * 1.45 + 48.0)))
        crop_h = int(min(frame_h, max(224.0, required_h, float(self.aim_fov_size) * 1.45 + 48.0)))
        crop_w = max(1, min(frame_w, crop_w))
        crop_h = max(1, min(frame_h, crop_h))
        left = int(round(center_x - crop_w * 0.5))
        top = int(round(center_y - crop_h * 0.5))
        left = min(max(0, left), max(0, frame_w - crop_w))
        top = min(max(0, top), max(0, frame_h - crop_h))
        roi = frame[top:top + crop_h, left:left + crop_w]
        if roi.size == 0:
            return frame, (0, 0)
        return roi, (left, top)

    def _predict_detection_rows(self, frame):
        """Run the latency-budgeted main pass plus an occasional cropped ROI zoom."""
        started = time.perf_counter()
        effective_confidence = max(
            float(self._target_confidence_floor),
            min(float(self.conf), 0.14),
        )
        self._last_effective_confidence = effective_confidence
        # Keep model weights and confidence semantics; input size adapts to measured latency.
        base_rows, base_size = self._run_detector_pass(frame, confidence_floor=0.045)
        self._last_base_inference_ms = float(getattr(self, '_last_detector_pass_ms', 0.0))
        self._update_detector_budget(self._last_base_inference_ms)
        self._last_zoom_used = False
        self._last_zoom_reason = 'not needed'
        zoom_rows = np.empty((0, 6), dtype=np.float32)
        zoom_size = None
        zoom_offset = (0, 0)

        if self._should_run_zoom_pass(base_rows, frame.shape):
            reason = self._last_zoom_reason
            active_lock = getattr(self, '_aim_locked_track_id', None) is not None
            uncertain_box = reason in ('weak box in FOV', 'no base detections')
            minimum_interval = (
                self._zoom_interval_uncertain_target if (uncertain_box or active_lock)
                else self._zoom_interval_empty_scene
            )
            # At 15 FPS, waiting twice the already conservative 280ms interval
            # can outlast several detector updates and starve acquisition. Keep
            # weak/no-detection FOV searches responsive; throttle empty-scene
            # searches more aggressively because they cannot confirm a target.
            runtime_fps = float(getattr(self, '_runtime_fps', 0.0) or 0.0)
            previous_inference_ms = float(getattr(self, '_last_inference_ms', 0.0) or 0.0)
            zoom_budget_factor = 1.0
            if not uncertain_box and not active_lock:
                if runtime_fps > 0.0 and runtime_fps < 18.0:
                    zoom_budget_factor = 1.20
                elif runtime_fps > 0.0 and runtime_fps < 32.0:
                    zoom_budget_factor = 1.10
                elif runtime_fps > 0.0 and runtime_fps < 48.0:
                    zoom_budget_factor = 1.05
            if previous_inference_ms > 48.0:
                zoom_budget_factor = max(zoom_budget_factor, 1.35 if (uncertain_box or active_lock) else 1.60)
            minimum_interval *= zoom_budget_factor
            now = time.perf_counter()
            if now - self._last_zoom_run_at >= minimum_interval:
                zoom_factor = 1.8
                zoom_roi, zoom_offset = self._make_zoom_roi(frame, base_rows)
                roi_h, roi_w = zoom_roi.shape[:2]
                enlarged = cv2.resize(
                    zoom_roi,
                    (max(1, int(round(roi_w * zoom_factor))),
                     max(1, int(round(roi_h * zoom_factor)))),
                    interpolation=cv2.INTER_LINEAR,
                )
                try:
                    zoom_rows, zoom_size = self._run_detector_pass(
                        enlarged, confidence_floor=0.045
                    )
                    self._last_zoom_used = True
                    self._last_zoom_run_at = time.perf_counter()
                except Exception as exc:
                    if not self._detector_zoom_warning_logged:
                        print(f'[WARN] Adaptive ROI zoom unavailable; base detections continue: {exc}', flush=True)
                        self._detector_zoom_warning_logged = True
            else:
                remaining_ms = max(0.0, minimum_interval - (now - self._last_zoom_run_at)) * 1000.0
                self._last_zoom_reason = f'{reason}; refresh in {remaining_ms:.0f}ms'

        rows = self._merge_multiscale_rows(base_rows, zoom_rows, 1.8, zoom_offset)
        self._last_detector_imgsz = base_size
        if zoom_size is not None:
            self._last_inference_scale = f'BASE + ROI ZOOM x1.8 ({base_size}/{zoom_size})'
        else:
            self._last_inference_scale = f'BASE ({base_size})'
        self._last_inference_ms = (time.perf_counter() - started) * 1000.0
        return rows

    @staticmethod
    def _boxes_overlap_as_same_target(first, second):
        ax1, ay1, ax2, ay2 = map(float, first)
        bx1, by1, bx2, by2 = map(float, second)
        iw = max(0.0, min(ax2, bx2) - max(ax1, bx1))
        ih = max(0.0, min(ay2, by2) - max(ay1, by1))
        intersection = iw * ih
        area_a = max(1.0, (ax2 - ax1) * (ay2 - ay1))
        area_b = max(1.0, (bx2 - bx1) * (by2 - by1))
        iou = intersection / max(1.0, area_a + area_b - intersection)
        acx, acy = (ax1 + ax2) * 0.5, (ay1 + ay2) * 0.5
        bcx, bcy = (bx1 + bx2) * 0.5, (by1 + by2) * 0.5
        center_distance = math.hypot(acx - bcx, acy - bcy)
        scale = max(12.0, min(math.hypot(ax2 - ax1, ay2 - ay1), math.hypot(bx2 - bx1, by2 - by1)))
        return iou >= 0.12 or center_distance <= 0.22 * scale

    def _exclude_self_from_tracking(self, self_boxes):
        """Purge any track/aim lock overlapping a manually identified local avatar."""
        if not self_boxes:
            return
        kept_tracks = []
        for track in self._detection_tracks:
            track_box = track.get('box')
            if track_box is None or any(
                self._boxes_overlap_as_same_target(track_box, box) for box in self_boxes
            ):
                continue
            kept_tracks.append(track)
        self._detection_tracks = kept_tracks

        if self._aim_locked_box is not None and any(
            self._boxes_overlap_as_same_target(self._aim_locked_box, box) for box in self_boxes
        ):
            self._reset_aim_tracking()
            self._cancel_aim_motion('LOCAL PLAYER IDENTIFIED')
            self._set_aim_diagnostics(
                decision='BLOCKED', reason='LOCAL PLAYER IDENTIFIED',
                target_id=None, tracking_weight=0.0,
                command_dx=0, command_dy=0,
                output_state='CANCELLED', output_reason='LOCAL PLAYER IDENTIFIED',
            )

    @staticmethod
    def _minimum_cost_track_assignment(cost_matrix):
        """Rectangular Hungarian assignment; returns one column per track row."""
        if not cost_matrix:
            return []
        row_count = len(cost_matrix)
        column_count = len(cost_matrix[0]) if row_count else 0
        if column_count == 0:
            return [-1] * row_count
        if any(len(row) != column_count for row in cost_matrix):
            raise ValueError('Track cost matrix must be rectangular.')

        # Hungarian's shortest augmenting-path form requires rows <= columns.
        transposed = row_count > column_count
        matrix = (
            [list(row) for row in zip(*cost_matrix)]
            if transposed else [list(row) for row in cost_matrix]
        )
        n = len(matrix)
        m = len(matrix[0])
        u = [0.0] * (n + 1)
        v = [0.0] * (m + 1)
        p = [0] * (m + 1)
        way = [0] * (m + 1)

        for i in range(1, n + 1):
            p[0] = i
            j0 = 0
            minv = [float('inf')] * (m + 1)
            used = [False] * (m + 1)
            while True:
                used[j0] = True
                i0 = p[j0]
                delta = float('inf')
                j1 = 0
                row = matrix[i0 - 1]
                for j in range(1, m + 1):
                    if used[j]:
                        continue
                    cur = float(row[j - 1]) - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
                if not math.isfinite(delta):
                    break
                for j in range(m + 1):
                    if used[j]:
                        u[p[j]] += delta
                        v[j] -= delta
                    else:
                        minv[j] -= delta
                j0 = j1
                if p[j0] == 0:
                    break
            while j0:
                j1 = way[j0]
                p[j0] = p[j1]
                j0 = j1

        row_to_col = [-1] * n
        for j in range(1, m + 1):
            if p[j] != 0:
                row_to_col[p[j] - 1] = j - 1
        if not transposed:
            return row_to_col
        # For a transposed matrix, original rows are columns of the solved matrix.
        result = [-1] * row_count
        for transposed_row, original_col in enumerate(row_to_col):
            if original_col >= 0:
                result[original_col] = transposed_row
        return result

    def _confirm_detection_tracks(self, records, now):
        """Associate detections globally using motion, overlap and shape continuity.

        The previous greedy pair loop could consume a detection for the locally
        cheapest pair and leave a better global assignment unavailable. Hungarian
        assignment resolves all visible tracks together; unmatched detections
        start new IDs and unmatched tracks remain briefly available for recovery.
        """
        now = float(now)
        tracks = [
            track for track in self._detection_tracks
            if now - float(track.get('last_seen', 0.0)) <= 0.62
        ]
        track_count = len(tracks)
        record_count = len(records)
        unmatched_cost = 1.48
        invalid_cost = 1000000.0
        cost_matrix = []
        pair_metrics = {}

        for ti, track in enumerate(tracks):
            last_seen = float(track.get('last_seen', now))
            last_frame_time = float(track.get('last_frame_time', last_seen))
            frame_dt = min(0.20, max(0.001, now - last_frame_time))
            gap_dt = min(0.16, max(0.001, now - last_seen))
            base_cx, base_cy = self._camera_transform_point(track['cx'], track['cy'])
            base_box = self._camera_transform_box(track['box'])

            vx = max(-3000.0, min(3000.0, float(track.get('vx', 0.0))))
            vy = max(-3000.0, min(3000.0, float(track.get('vy', 0.0))))
            speed = math.hypot(vx, vy)
            # Predict only for identity association. Aim coordinates still come
            # from the current YOLO observation, never from this prediction.
            max_shift = 210.0
            shift_x = max(-max_shift, min(max_shift, vx * gap_dt * 0.82))
            shift_y = max(-max_shift, min(max_shift, vy * gap_dt * 0.82))
            assoc_box = (
                base_box[0] + shift_x, base_box[1] + shift_y,
                base_box[2] + shift_x, base_box[3] + shift_y,
            )
            assoc_cx = base_cx + shift_x
            assoc_cy = base_cy + shift_y
            old_w = max(1.0, base_box[2] - base_box[0])
            old_h = max(1.0, base_box[3] - base_box[1])
            old_area = max(1.0, old_w * old_h)
            old_aspect = old_w / old_h
            diagonal = math.hypot(old_w, old_h)
            gate = max(38.0, min(290.0, diagonal * 1.42 + 24.0 + min(170.0, speed * gap_dt * 1.32)))

            track['_flow_base_center'] = (base_cx, base_cy)
            track['_flow_frame_dt'] = frame_dt
            track['_flow_gap_dt'] = gap_dt
            track['_flow_center'] = (base_cx, base_cy)
            track['_flow_box'] = base_box
            track['_association_center'] = (assoc_cx, assoc_cy)
            track['_association_box'] = assoc_box
            track['_association_speed'] = speed

            row_costs = []
            for ri, record in enumerate(records):
                if int(record.get('class_id', -1)) != int(track.get('class_id', -2)):
                    row_costs.append(invalid_cost)
                    continue
                cx, cy = map(float, record['center'])
                distance = math.hypot(cx - assoc_cx, cy - assoc_cy)
                iou = self._aim_box_iou(assoc_box, record['box'])
                x1, y1, x2, y2 = map(float, record['box'])
                new_w = max(1.0, x2 - x1)
                new_h = max(1.0, y2 - y1)
                new_area = max(1.0, new_w * new_h)
                size_penalty = abs(math.log(new_area / old_area))
                aspect_penalty = abs(math.log(max(1e-4, (new_w / new_h) / max(1e-4, old_aspect))))

                if distance > gate and iou < 0.055:
                    row_costs.append(invalid_cost)
                    continue
                if size_penalty > 1.35 and iou < 0.16:
                    row_costs.append(invalid_cost)
                    continue
                if aspect_penalty > 0.95 and iou < 0.18:
                    row_costs.append(invalid_cost)
                    continue

                normalized_distance = min(2.0, distance / max(1.0, gate))
                score = (
                    0.60 * normalized_distance
                    + 0.73 * (1.0 - iou)
                    + 0.10 * min(2.0, size_penalty)
                    + 0.07 * min(2.0, aspect_penalty)
                )
                row_costs.append(score)
                pair_metrics[(ti, ri)] = (distance, iou, size_penalty, aspect_penalty, score)

            # One private dummy column per track lets the optimizer leave a
            # track unmatched instead of forcing a bad identity transfer.
            row_costs.extend(unmatched_cost + (0.00001 * abs(ti - dj)) for dj in range(track_count))
            cost_matrix.append(row_costs)

        assignment = self._minimum_cost_track_assignment(cost_matrix) if track_count else []
        assigned = {}
        used_tracks = set()
        for ti, col in enumerate(assignment):
            if col < 0 or col >= record_count:
                continue
            metrics = pair_metrics.get((ti, col))
            if metrics is None:
                continue
            distance, iou, size_penalty, aspect_penalty, score = metrics
            if score >= unmatched_cost:
                continue
            # Refuse genuinely ambiguous one-to-many matches unless the chosen
            # observation overlaps strongly; this prevents ID swaps at crossings.
            alternatives = sorted(
                pair_metrics[(ti, ri)][4]
                for ri in range(record_count)
                if (ti, ri) in pair_metrics and ri != col
            )
            if alternatives and abs(alternatives[0] - score) < 0.055 and iou < 0.18:
                continue
            assigned[col] = ti
            used_tracks.add(ti)

        for ti, track in enumerate(tracks):
            if ti not in used_tracks:
                track['misses'] = int(track.get('misses', 0)) + 1
                track['vx'] = max(-3000.0, min(3000.0, float(track.get('vx', 0.0)) * 0.88))
                track['vy'] = max(-3000.0, min(3000.0, float(track.get('vy', 0.0)) * 0.88))
                track['box'] = track['_flow_box']
                track['cx'], track['cy'] = track['_flow_center']
                track['last_frame_time'] = now

        next_tracks = list(tracks)
        for ri, record in enumerate(records):
            if ri in assigned:
                ti = assigned[ri]
                track = next_tracks[ti]
                cx, cy = map(float, record['center'])
                base_cx, base_cy = track['_flow_base_center']
                measurement_dt = min(0.22, max(0.001, now - float(track['last_seen'])))
                measured_vx = max(-3400.0, min(3400.0, (cx - base_cx) / measurement_dt))
                measured_vy = max(-3400.0, min(3400.0, (cy - base_cy) / measurement_dt))
                measured_speed = math.hypot(measured_vx, measured_vy)
                velocity_alpha = 0.70 if measured_speed >= 420.0 else 0.42
                track['vx'] = max(-3400.0, min(3400.0,
                    (1.0 - velocity_alpha) * float(track.get('vx', 0.0)) + velocity_alpha * measured_vx))
                track['vy'] = max(-3400.0, min(3400.0,
                    (1.0 - velocity_alpha) * float(track.get('vy', 0.0)) + velocity_alpha * measured_vy))
                track['hits'] = min(10, int(track.get('hits', 1)) + 1) if now - float(track['last_seen']) <= 0.50 else 1
                track['misses'] = 0
                track['box'] = record['box']
                track['cx'], track['cy'] = cx, cy
                track['last_seen'] = now
                track['last_frame_time'] = now
                track['confidence'] = float(record.get('confidence', 0.0))
            else:
                cx, cy = map(float, record['center'])
                track = {
                    'id': self._next_detection_track_id,
                    'class_id': int(record['class_id']),
                    'box': record['box'],
                    'cx': cx, 'cy': cy,
                    'vx': 0.0, 'vy': 0.0,
                    'hits': 1, 'misses': 0,
                    'last_seen': now,
                    'last_frame_time': now,
                    'confidence': float(record.get('confidence', 0.0)),
                }
                self._next_detection_track_id += 1
                next_tracks.append(track)
                assigned[ri] = len(next_tracks) - 1

            matched_track = next_tracks[assigned[ri]]
            for key in ('_flow_base_center', '_flow_frame_dt', '_flow_gap_dt', '_flow_center',
                        '_flow_box', '_association_center', '_association_box', '_association_speed'):
                matched_track.pop(key, None)
            record['track_id'] = matched_track['id']
            # High-confidence first observations may acquire immediately; normal
            # detections still require two consistent frames to reject flicker.
            record['confirmed'] = (
                int(matched_track.get('hits', 1)) >= 2
                or (int(matched_track.get('hits', 1)) == 1 and float(record.get('confidence', 0.0)) >= 0.82)
            )

        for track in next_tracks:
            for key in ('_flow_base_center', '_flow_frame_dt', '_flow_gap_dt', '_flow_center',
                        '_flow_box', '_association_center', '_association_box', '_association_speed'):
                track.pop(key, None)
        self._detection_tracks = next_tracks
        return records

    def _find_locked_aim_observation(self, observations, now):
        """Continue the locked target across brief detector-ID changes.

        The detector's local track ID is useful evidence, but it can change
        after a missed frame or an association conflict. A new ID is therefore
        accepted only when class and box geometry support an unambiguous
        continuation of the existing lock.
        """
        if self._aim_locked_box is None or self._aim_track_center_x is None:
            return None

        last_time = self._aim_track_last_time or now
        elapsed = min(0.80, max(0.001, now - last_time))
        flow_x, flow_y = self._camera_transform_point(
            self._aim_track_center_x, self._aim_track_center_y
        )
        # During a brief detector-ID handoff, compare candidates against the
        # last measured target motion as well as camera flow. The lead is short
        # and bounded so it cannot turn into an unbounded extrapolated lock.
        association_lead_time = min(0.045, elapsed)
        velocity_x = max(-2200.0, min(2200.0, float(getattr(self, '_aim_track_velocity_x', 0.0) or 0.0)))
        velocity_y = max(-2200.0, min(2200.0, float(getattr(self, '_aim_track_velocity_y', 0.0) or 0.0)))
        box_diag = math.hypot(
            max(1.0, float(self._aim_locked_box[2]) - float(self._aim_locked_box[0])),
            max(1.0, float(self._aim_locked_box[3]) - float(self._aim_locked_box[1])),
        )
        association_cap = max(18.0, min(70.0, box_diag * 0.75))
        expected_x = flow_x + max(-association_cap, min(association_cap, velocity_x * association_lead_time))
        expected_y = flow_y + max(-association_cap, min(association_cap, velocity_y * association_lead_time))
        ox1, oy1, ox2, oy2 = map(float, self._camera_transform_box(self._aim_locked_box))
        old_w = max(1.0, ox2 - ox1)
        old_h = max(1.0, oy2 - oy1)
        diagonal = math.hypot(old_w, old_h)
        track_limit = max(
            24.0,
            min(190.0, diagonal * 1.35 + 18.0 + min(60.0, elapsed * 45.0)),
        )
        old_area = max(1.0, old_w * old_h)
        locked_id = getattr(self, '_aim_locked_track_id', None)
        locked_track = next((track for track in self._detection_tracks
                             if track.get('id') == locked_id), None) if locked_id is not None else None
        locked_speed = math.hypot(
            float(locked_track.get('vx', 0.0)),
            float(locked_track.get('vy', 0.0)),
        ) if locked_track is not None else 0.0
        locked_class_id = getattr(self, '_aim_locked_class_id', None)
        if locked_class_id is None and locked_track is not None:
            locked_class_id = locked_track.get('class_id')

        same_id_candidate = None
        reassociation_candidates = []
        untracked_candidates = []

        for item in observations:
            cx, cy = map(float, item['box_center'])
            distance = math.hypot(cx - expected_x, cy - expected_y)
            iou = self._aim_box_iou((ox1, oy1, ox2, oy2), item['box'])
            x1, y1, x2, y2 = map(float, item['box'])
            new_area = max(1.0, (x2 - x1) * (y2 - y1))
            size_penalty = abs(math.log(new_area / old_area))
            item_track_id = item.get('track_id')
            same_id = locked_id is not None and item_track_id == locked_id

            item_class_id = item.get('class_id')
            if locked_class_id is not None and item_class_id is not None:
                try:
                    if int(item_class_id) != int(locked_class_id):
                        continue
                except (TypeError, ValueError):
                    if str(item_class_id) != str(locked_class_id):
                        continue

            if same_id:
                # Keep the current ID when its observation remains geometrically
                # plausible. A same-ID match is not trusted blindly.
                distance_gate = min(310.0, max(124.0, track_limit + locked_speed * 0.055))
                overlap_gate = 0.10 if locked_class_id is None else 0.035
                size_gate = 0.78 if locked_class_id is None else 1.18
                plausible = (
                    (distance <= distance_gate or iou >= overlap_gate)
                    and (size_penalty <= size_gate or iou >= 0.24)
                )
                if plausible:
                    score = (distance / max(1.0, track_limit)) + (0.82 * (1.0 - iou)) + (0.15 * size_penalty)
                    same_id_candidate = (score, item, distance, iou, size_penalty)
                continue

            if locked_id is not None:
                # Reassociation is deliberately tighter than normal detector
                # tracking. It prevents a nearby same-class opponent from
                # stealing the lock while allowing a genuine ID handoff.
                reassociation_gate = max(
                    28.0,
                    min(180.0, diagonal * 1.15 + 18.0 + min(42.0, elapsed * 28.0)),
                )
                plausible = (
                    (distance <= reassociation_gate or iou >= 0.16)
                    and (size_penalty <= 0.55 or iou >= 0.30)
                )
                if not plausible:
                    continue
                normalized_distance = min(2.0, distance / max(1.0, reassociation_gate))
                score = (
                    0.70 * normalized_distance
                    + 0.72 * (1.0 - iou)
                    + 0.16 * size_penalty
                )
                if score <= 1.32:
                    reassociation_candidates.append((score, item, distance, iou, size_penalty))
                continue

            # Preserve the legacy no-ID fallback for older/custom detectors.
            plausible = (
                (distance <= track_limit or iou >= 0.10)
                and (size_penalty <= 0.78 or iou >= 0.24)
            )
            if plausible:
                score = (distance / max(1.0, track_limit)) + (0.82 * (1.0 - iou)) + (0.15 * size_penalty)
                untracked_candidates.append((score, item, distance, iou, size_penalty))

        if same_id_candidate is not None:
            return same_id_candidate[1]

        if locked_id is not None:
            if not reassociation_candidates:
                return None
            reassociation_candidates.sort(key=lambda value: value[0])
            best = reassociation_candidates[0]
            best_score, candidate, _distance, best_iou, best_size_penalty = best

            # A fresh track normally needs two observations. One frame is enough
            # only when the new box strongly overlaps the previous locked box.
            _best_distance = reassociation_candidates[0][2]
            continuity_gate = max(18.0, min(46.0, diagonal * 0.30))
            strong_overlap = best_iou >= 0.38 and best_size_penalty <= 0.55
            strong_motion_continuation = (
                _best_distance <= continuity_gate
                and best_size_penalty <= 0.42
                and best_score <= 0.92
            )
            if not bool(candidate.get('confirmed', False)) and not strong_overlap and not strong_motion_continuation:
                return None

            # When two new IDs fit almost equally well, retain the lock in a
            # waiting state rather than jumping between adjacent targets.
            if len(reassociation_candidates) > 1:
                second_score = reassociation_candidates[1][0]
                if (second_score - best_score) < 0.16 and best_iou < 0.55:
                    return None
            return candidate

        if not untracked_candidates:
            return None
        untracked_candidates.sort(key=lambda value: value[0])
        score, candidate, _distance, _iou, _size_penalty = untracked_candidates[0]
        return candidate if score <= 1.55 else None

    def _update_locked_aim_measurement(self, item, now, new_target=False):
        """Follow the same normalized point inside the live hitbox.

        The point is captured from the reticle when a target is acquired, and
        recaptured only when physical mouse input indicates deliberate manual
        repositioning. Otherwise it moves with the target's resized box, so a
        jump or strafe cannot leave the controller aiming at an old screen pixel.
        """
        box = tuple(float(v) for v in item['box'])
        cx, cy = map(float, item['box_center'])
        head_mode = str(getattr(self, 'aim_target_mode', 'all')).lower() == 'head'
        if head_mode:
            # In HEAD mode, use the user-positioned point relative to the raw
            # detector box. The four ALL hitbox-edge controls do not affect it.
            left, top, right, bottom = box
        else:
            left, top, right, bottom = self._scaled_hitbox_bounds(*box)
        box_w = max(1.0, right - left)
        box_h = max(1.0, bottom - top)
        reticle_x = float(self.crosshair_x)
        reticle_y = float(self.crosshair_y)
        reticle_inside = left <= reticle_x <= right and top <= reticle_y <= bottom

        manual_retarget = bool(
            not head_mode
            and self.manual_input_priority
            and self._manual_mouse_active
            and self._raw_mouse_input_available
        )
        if head_mode:
            # The draggable green point is global and normalized inside every
            # detected player box; manual mouse movement must not replace it.
            self._aim_point_u = max(0.0, min(1.0, float(self.aim_point_u)))
            self._aim_point_v = max(0.0, min(1.0, float(self.aim_point_v)))
            self._aim_anchor_mode = 'HEAD'
            capture_anchor = False
        else:
            anchor_missing = self._aim_point_u is None or self._aim_point_v is None
            capture_anchor = bool(new_target or anchor_missing or manual_retarget)

        if capture_anchor:
            if new_target and not head_mode:
                # Acquire a stable body reference instead of freezing the
                # reticle's current pixel or the nearest hitbox edge.
                self._aim_point_u = 0.50
                self._aim_point_v = 0.50
                self._aim_anchor_mode = 'CENTER'
            else:
                # Preserve deliberate manual placement when retargeting an
                # already-locked hitbox.
                point_x = min(right, max(left, reticle_x))
                point_y = min(bottom, max(top, reticle_y))
                self._aim_point_u = max(0.0, min(1.0, (point_x - left) / box_w))
                self._aim_point_v = max(0.0, min(1.0, (point_y - top) / box_h))
                self._aim_anchor_mode = 'INSIDE' if reticle_inside else 'EDGE'

        # This is the core of the lock: preserve the same relative point as the
        # detected hitbox changes size or position from frame to frame.
        point_u = max(0.0, min(1.0, float(self._aim_point_u)))
        point_v = max(0.0, min(1.0, float(self._aim_point_v)))
        measured_x = left + point_u * box_w
        measured_y = top + point_v * box_h
        anchor_mode = 'HEAD' if head_mode else str(
            self._aim_anchor_mode or ('INSIDE' if reticle_inside else 'EDGE')
        )

        had_detection_gap = self._aim_lock_lost_since is not None
        reset_point = (
            new_target
            or self._aim_track_center_x is None
            or self._filtered_aim_x is None
            or self._filtered_aim_y is None
        )

        if reset_point:
            self._aim_first_lock_at = float(now)
            self._aim_first_lock_target_id = item.get('track_id')
            self._aim_track_center_x, self._aim_track_center_y = cx, cy
            self._aim_track_velocity_x = 0.0
            self._aim_track_velocity_y = 0.0
            self._filtered_aim_x, self._aim_point_velocity_x = self._one_euro_point(
                'x', measured_x, now, reset=True
            )
            self._filtered_aim_y, self._aim_point_velocity_y = self._one_euro_point(
                'y', measured_y, now, reset=True
            )
            self._aim_track_last_time = now
            self._aim_point_last_time = now
            self._aim_remainder_x = 0.0
            self._aim_remainder_y = 0.0
            self._aim_last_update_time = None
            self._stable_prev_error_x = None
            self._stable_prev_error_y = None
            self._stable_inside_deadzone = False
            self._aim_previous_error_x = None
            self._aim_previous_error_y = None
            self._aim_axis_gain_x = 1.0
            self._aim_axis_gain_y = 1.0
            self._last_aim_gain_factor = 1.0
            self._reset_new_controller_state()
            self._cancel_aim_motion('NEW TARGET LOCK')
        else:
            # A fresh box ends any detector gap. Discard old object velocity;
            # never let a stale estimate influence the resumed lock.
            if had_detection_gap:
                self._aim_track_velocity_x = 0.0
                self._aim_track_velocity_y = 0.0
                self._aim_point_velocity_x = 0.0
                self._aim_point_velocity_y = 0.0
            # Transform the previous point through global camera motion first,
            # then measure only the target's newly detected residual movement.
            self._compensate_aim_point_for_camera_motion()
            track_dt = min(0.12, max(0.001, now - (self._aim_track_last_time or now)))
            flow_cx, flow_cy = self._camera_transform_point(
                self._aim_track_center_x, self._aim_track_center_y
            )
            measured_vx = (cx - flow_cx) / track_dt
            measured_vy = (cy - flow_cy) / track_dt
            if abs(measured_vx) < 5.0:
                measured_vx = 0.0
            if abs(measured_vy) < 5.0:
                measured_vy = 0.0
            self._aim_track_velocity_x = max(-2200.0, min(2200.0,
                0.62 * self._aim_track_velocity_x + 0.38 * measured_vx))
            self._aim_track_velocity_y = max(-2200.0, min(2200.0,
                0.62 * self._aim_track_velocity_y + 0.38 * measured_vy))
            self._aim_track_center_x, self._aim_track_center_y = cx, cy
            self._aim_track_last_time = now

            if manual_retarget:
                # A deliberate physical mouse movement changes the selected
                # location within the hitbox. Drop only obsolete queued output;
                # normal closed-loop tracking resumes on the next fresh frame.
                self._cancel_aim_motion('MANUAL POINT RESELECTED')
                self._filtered_aim_x = measured_x
                self._filtered_aim_y = measured_y
                self._aim_point_velocity_x = 0.0
                self._aim_point_velocity_y = 0.0
                self._aim_point_filter_x = {
                    'raw': measured_x, 'filtered': measured_x,
                    'derivative': 0.0, 'time': float(now),
                }
                self._aim_point_filter_y = {
                    'raw': measured_y, 'filtered': measured_y,
                    'derivative': 0.0, 'time': float(now),
                }
                self._aim_remainder_x = 0.0
                self._aim_remainder_y = 0.0
                self._stable_prev_error_x = None
                self._stable_prev_error_y = None
                self._stable_inside_deadzone = False
            else:
                # A reacquired detection starts a new measurement segment.
                # Reset the filter so its old derivative cannot carry momentum
                # from the stale pre-occlusion observation into the new lock.
                self._filtered_aim_x, self._aim_point_velocity_x = self._one_euro_point(
                    'x', measured_x, now, reset=had_detection_gap
                )
                self._filtered_aim_y, self._aim_point_velocity_y = self._one_euro_point(
                    'y', measured_y, now, reset=had_detection_gap
                )
            self._aim_point_last_time = now

        self._aim_anchor_mode = anchor_mode
        self._aim_locked_box = box
        self._aim_locked_hitbox = (left, top, right, bottom)
        self._aim_locked_track_id = item.get('track_id')
        try:
            self._aim_locked_class_id = int(item.get('class_id'))
        except (TypeError, ValueError):
            self._aim_locked_class_id = None
        self._aim_locked_part = 'head-point' if head_mode else 'hitbox-relative'
        self._aim_lock_misses = 0
        self._aim_lock_lost_since = None
        self._aim_track_state = 'LOCKED'
        self._aim_last_valid_measurement_time = float(now)
        result = dict(item)
        result['x'] = self._filtered_aim_x
        result['y'] = self._filtered_aim_y
        result['part'] = 'head-point' if head_mode else 'hitbox-relative'
        result['anchor_mode'] = anchor_mode
        result['point_u'] = point_u
        result['point_v'] = point_v
        result['tracking_state'] = self._aim_track_state
        return result

    @staticmethod
    def _is_player_moving():
        """Detect common keyboard movement inputs (WASD and arrow keys)."""
        movement_keys = (0x57, 0x41, 0x53, 0x44, 0x25, 0x26, 0x27, 0x28)
        try:
            return any(bool(win32api.GetAsyncKeyState(key) & 0x8000) for key in movement_keys)
        except Exception:
            return False

    @staticmethod
    def _target_motion_explains_crossing(previous_error, current_error, target_velocity, dt):
        """Return True when observed target motion alone crosses the reticle.

        The detector measures the target in screen coordinates. A moving target
        can cross the reticle even when the controller did not overshoot. Treating
        every error sign change as controller overshoot suppresses tracking at
        exactly the moment a target strafes or jumps across the aim point.
        """
        if previous_error is None:
            return False
        try:
            previous_error = float(previous_error)
            current_error = float(current_error)
            target_velocity = float(target_velocity)
            dt = max(0.0, min(0.12, float(dt)))
            if not all(math.isfinite(v) for v in (previous_error, current_error, target_velocity, dt)):
                return False
            if previous_error * current_error > 0.0:
                return False
            target_only_error = previous_error + target_velocity * dt
            return previous_error * target_only_error <= 0.0
        except (TypeError, ValueError, OverflowError):
            return False

    def _update_axis_gain(self, axis, error, target_crossed=False):
        """Reduce gain only for a real controller-caused overshoot.

        A target-driven sign change is not an overshoot and must not lower gain.
        """
        if axis == 'x':
            previous = self._aim_previous_error_x
            gain = self._aim_axis_gain_x
        else:
            previous = self._aim_previous_error_y
            gain = self._aim_axis_gain_y

        if previous is not None:
            crossed = previous * error < 0 and abs(previous) > 1.8 and abs(error) > 1.8
            if crossed and not target_crossed:
                gain = max(0.22, gain * 0.52)
            elif crossed and target_crossed:
                # Keep the full response when the character itself crossed the
                # reticle; recover gently if an earlier genuine overshoot reduced it.
                gain = min(1.0, gain + 0.025)
            elif previous * error > 0 and abs(error) < abs(previous) * 0.92:
                gain = min(1.0, gain + 0.010)

        if axis == 'x':
            self._aim_previous_error_x = float(error)
            self._aim_axis_gain_x = gain
        else:
            self._aim_previous_error_y = float(error)
            self._aim_axis_gain_y = gain
        return gain

    def _body_anchors(self, x1, y1, x2, y2):
        """Return approximate body points from a YOLO bounding box.

        The model supplies boxes rather than pose landmarks, so these points
        are geometric estimates and should be treated as approximate.
        """
        width = max(1.0, float(x2 - x1))
        height = max(1.0, float(y2 - y1))
        mid_x = (float(x1) + float(x2)) / 2.0
        return (
            ('head', mid_x, float(y1) + height * 0.13),
            ('left arm', float(x1) + width * 0.16, float(y1) + height * 0.34),
            ('right arm', float(x2) - width * 0.16, float(y1) + height * 0.34),
            ('upper torso', mid_x, float(y1) + height * 0.39),
            ('lower torso', mid_x, float(y1) + height * 0.57),
            ('pelvis', mid_x, float(y1) + height * 0.69),
        )

    def _choose_body_anchor(self, x1, y1, x2, y2, preferred_part=None):
        """Choose the nearest estimated body point with a small switch hysteresis.

        A previous version hard-locked the first point (often an arm) for as
        long as the same player box was tracked. That made it impossible for
        the selected point to move to the head. This version keeps the current
        point only while it is nearly as close as the best candidate; it can
        switch when another point becomes clearly nearer.
        """
        anchors = self._body_anchors(x1, y1, x2, y2)

        def distance(item):
            return math.hypot(
                item[1] - self.crosshair_x,
                item[2] - self.crosshair_y,
            )

        best = min(anchors, key=distance)
        if preferred_part:
            current = next((item for item in anchors if item[0] == preferred_part), None)
            if current is not None and current[0] != best[0]:
                height = max(1.0, float(y2 - y1))
                switch_margin = max(2.0, min(8.0, height * 0.035))
                # Avoid tiny frame-to-frame flips, but allow a deliberate
                # movement toward another body region (e.g. arm -> head).
                if distance(best) + switch_margin >= distance(current):
                    return current
        return best

    def _send_aim_delta(self, dx, dy, record_feedback=True):
        """Send one relative mouse step; feedback can be batched by the worker."""
        dx = int(dx)
        dy = int(dy)
        if not (dx or dy):
            return
        method = self.mouse_method.lower()
        if method == 'ddxoft' and Aimbot.mouse_dll is not None:
            Aimbot.mouse_dll.DD_movR(dx, dy)
        elif method == 'makcu' and Aimbot.makcu is not None:
            Aimbot.makcu.move(dx, dy)
        else:
            ctypes.windll.user32.mouse_event(0x0001, dx, dy, 0, 0)
        if record_feedback:
            self._record_aim_output_feedback(dx, dy)

    def _record_aim_output_feedback(self, dx, dy, sent_at=None):
        """Store one aggregate movement sample, not one Python lock per micro-step."""
        sent_at = time.perf_counter() if sent_at is None else float(sent_at)
        try:
            with self._adaptive_feedback_lock:
                self._adaptive_output_history.append((sent_at, int(dx), int(dy)))
                if len(self._adaptive_output_history) > 320:
                    cutoff = sent_at - 0.40
                    self._adaptive_output_history = [
                        item for item in self._adaptive_output_history if item[0] >= cutoff
                    ][-256:]
        except Exception:
            pass

    def _set_aim_diagnostics(self, **values):
        """Update the shared telemetry consumed by both Debug windows."""
        lock = getattr(self, '_aim_diag_lock', None)
        if lock is None:
            self._aim_diagnostics = dict(getattr(self, '_aim_diagnostics', {}), **values)
            return
        with lock:
            self._aim_diagnostics.update(values)

    def _get_aim_diagnostics(self):
        lock = getattr(self, '_aim_diag_lock', None)
        if lock is None:
            return dict(getattr(self, '_aim_diagnostics', {}))
        with lock:
            return dict(self._aim_diagnostics)

    def _open_aim_motion_timer(self):
        """Create a high-resolution waitable timer for smooth sub-millisecond steps.

        Windows' default sleep granularity can turn many tiny steps into a slow
        queue. A high-resolution waitable timer keeps the same short response
        window while spacing the relative mouse deltas more evenly. If the API
        is unavailable, the worker uses a short performance-counter wait.
        """
        try:
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            create = kernel32.CreateWaitableTimerExW
            create.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
            create.restype = wintypes.HANDLE
            # CREATE_WAITABLE_TIMER_HIGH_RESOLUTION; TIMER_ALL_ACCESS.
            handle = create(None, None, 0x00000002, 0x001F0003)
            if not handle:
                return False

            set_timer = kernel32.SetWaitableTimer
            set_timer.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong), wintypes.LONG,
                ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
            ]
            set_timer.restype = wintypes.BOOL
            wait = kernel32.WaitForSingleObject
            wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            wait.restype = wintypes.DWORD
            close = kernel32.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL

            self._aim_motion_timer_kernel32 = kernel32
            self._aim_motion_timer_handle = handle
            self._aim_motion_timer_high_resolution = True
            return True
        except Exception:
            self._aim_motion_timer_kernel32 = None
            self._aim_motion_timer_handle = None
            self._aim_motion_timer_high_resolution = False
            return False

    def _close_aim_motion_timer(self):
        handle = getattr(self, '_aim_motion_timer_handle', None)
        kernel32 = getattr(self, '_aim_motion_timer_kernel32', None)
        if handle and kernel32 is not None:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
        self._aim_motion_timer_handle = None
        self._aim_motion_timer_kernel32 = None
        self._aim_motion_timer_high_resolution = False

    def _wait_aim_motion_interval(self, delay):
        """Wait for one micro-step interval without a 1 ms sleep quantization."""
        delay = max(0.0, float(delay))
        if delay <= 0.0:
            return self._aim_motion_stop.is_set()

        handle = getattr(self, '_aim_motion_timer_handle', None)
        kernel32 = getattr(self, '_aim_motion_timer_kernel32', None)
        if handle and kernel32 is not None:
            try:
                # Negative 100 ns units represent a relative due time.
                due = ctypes.c_longlong(-max(1, int(round(delay * 10_000_000.0))))
                if kernel32.SetWaitableTimer(handle, ctypes.byref(due), 0, None, None, False):
                    result = kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
                    if result == 0:
                        return self._aim_motion_stop.is_set()
            except Exception:
                pass

        # CPU-friendly fallback. Event.wait releases the GIL and yields the
        # core instead of busy-spinning while a micro-step interval elapses.
        return self._aim_motion_stop.wait(delay)

    def _aim_motion_worker_loop(self):
        """Dispatch small mouse increments with low per-step Python overhead."""
        self._open_aim_motion_timer()
        self._set_aim_diagnostics(
            worker_state='RUNNING',
            output_reason=('HIGH-RES TIMER' if self._aim_motion_timer_high_resolution else 'YIELDED WAIT FALLBACK'),
        )
        try:
            while not self._aim_motion_stop.is_set():
                self._aim_motion_wakeup.wait()
                with self._aim_motion_lock:
                    command = self._aim_motion_pending
                    self._aim_motion_pending = None
                    self._aim_motion_wakeup.clear()
                if self._aim_motion_stop.is_set():
                    break
                if command is None:
                    continue

                sequence = command['sequence']
                steps = command['steps']
                step_delay = command['step_delay']
                target_id = command.get('target_id')
                expires_at = command['expires_at']
                sent_dx = sent_dy = 0
                sent_steps = 0
                cancelled_reason = ''

                for step_x, step_y in steps:
                    if self._aim_motion_stop.is_set():
                        cancelled_reason = 'RUNTIME STOPPED'
                        break
                    with self._aim_motion_lock:
                        current_sequence = self._aim_motion_sequence
                    if sequence != current_sequence:
                        cancelled_reason = 'SUPERSEDED BY FRESH FRAME'
                        break
                    if time.perf_counter() > expires_at:
                        cancelled_reason = 'COMMAND EXPIRED'
                        self._cancel_aim_motion_if_current(sequence, cancelled_reason)
                        break
                    if not (getattr(self, 'master_enabled', False) and getattr(self, 'aim_enabled', False)):
                        cancelled_reason = 'AIM NOT ACTIVE'
                        self._cancel_aim_motion_if_current(sequence, cancelled_reason)
                        break
                    if target_id != getattr(self, '_aim_locked_track_id', None):
                        cancelled_reason = 'TARGET CHANGED'
                        self._cancel_aim_motion_if_current(sequence, cancelled_reason)
                        break

                    measurement_time = getattr(self, '_aim_last_valid_measurement_time', None)
                    if (measurement_time is None
                            or (time.perf_counter() - float(measurement_time)) > 0.14):
                        cancelled_reason = 'NO FRESH DETECTION'
                        self._cancel_aim_motion_if_current(sequence, cancelled_reason)
                        break
                    point_x = getattr(self, '_filtered_aim_x', None)
                    point_y = getattr(self, '_filtered_aim_y', None)
                    if (point_x is None or point_y is None
                            or not self._aim_point_inside_fov(point_x, point_y)):
                        cancelled_reason = 'OUTSIDE FOV'
                        self._cancel_aim_motion_if_current(sequence, cancelled_reason)
                        break

                    try:
                        self._send_aim_delta(step_x, step_y, record_feedback=False)
                    except Exception as exc:
                        cancelled_reason = f'OUTPUT ERROR: {type(exc).__name__}'
                        break
                    sent_dx += step_x
                    sent_dy += step_y
                    sent_steps += 1
                    if step_delay > 0 and self._wait_aim_motion_interval(step_delay):
                        cancelled_reason = 'RUNTIME STOPPED'
                        break

                if sent_steps:
                    sent_at = time.perf_counter()
                    self._record_aim_output_feedback(sent_dx, sent_dy, sent_at)
                    with self._aim_motion_lock:
                        still_current = sequence == self._aim_motion_sequence
                    output_state = 'SENT' if still_current and not cancelled_reason else 'PARTIAL'
                    output_reason = 'MOUSE API CALLED' if output_state == 'SENT' else (cancelled_reason or 'SUPERSEDED')
                    self._set_aim_diagnostics(
                        queue_state='EMPTY' if still_current else 'REPLACED',
                        queue_steps=0,
                        output_state=output_state,
                        output_reason=output_reason[:32],
                        last_dx=sent_dx, last_dy=sent_dy,
                        last_output_at=sent_at,
                    )
                elif cancelled_reason and cancelled_reason != 'SUPERSEDED BY FRESH FRAME':
                    self._set_aim_diagnostics(
                        queue_state='EMPTY', queue_steps=0,
                        output_state='CANCELLED', output_reason=cancelled_reason[:32],
                    )
        finally:
            self._close_aim_motion_timer()
            self._set_aim_diagnostics(worker_state='STOPPED')

    def _start_aim_motion_worker(self):
        if self._aim_motion_thread and self._aim_motion_thread.is_alive():
            return
        self._aim_motion_stop.clear()
        self._aim_motion_wakeup.clear()
        self._aim_motion_thread = threading.Thread(
            target=self._aim_motion_worker_loop,
            name='LunarSmoothMouseOutput',
            daemon=True,
        )
        self._aim_motion_thread.start()

    def _submit_aim_motion(self, dx, dy, duration, target_id=None):
        """Publish one atomic correction per fresh frame.

        Splitting each correction into many 2-count events made commands span
        several frames. New detections then superseded commands halfway through,
        producing partial movements and visible wobble. The controller now owns
        smoothing; the output worker only dispatches the latest bounded delta.
        """
        dx = int(dx)
        dy = int(dy)
        if not (dx or dy):
            self._set_aim_diagnostics(command_dx=0, command_dy=0,
                                      queue_state='EMPTY', queue_steps=0,
                                      output_state='IDLE', output_reason='SUBPIXEL')
            return

        # Dispatch one atomic relative delta. Per-frame splitting added latency
        # and could leave the correction partially applied when a fresh frame
        # superseded the previous command.
        steps = [(dx, dy)]
        step_delay = 0.0
        motion_window = 0.0
        created_at = time.perf_counter()
        expires_at = created_at + 0.045
        with self._aim_motion_lock:
            self._aim_motion_sequence += 1
            sequence = self._aim_motion_sequence
            self._aim_motion_pending = {
                'sequence': sequence,
                'steps': steps,
                'step_delay': step_delay,
                'target_id': target_id,
                'created_at': created_at,
                'expires_at': expires_at,
            }
        self._set_aim_diagnostics(
            decision='ALLOW', reason='TARGET VALID', target_id=target_id,
            command_dx=dx, command_dy=dy,
            queue_state='QUEUED', queue_steps=1,
            output_state='PENDING', output_reason='IMMEDIATE SINGLE-DELTA',
            output_window_ms=motion_window * 1000.0,
            output_step_px=float(max((abs(x) + abs(y)) for x, y in steps)),
            last_command_at=created_at, last_command_expires_at=expires_at,
        )
        self._aim_motion_wakeup.set()

    def _cancel_aim_motion_if_current(self, sequence, reason):
        """Cancel one expired command without clearing a newer frame's queue."""
        with self._aim_motion_lock:
            if sequence != self._aim_motion_sequence:
                return False
            self._aim_motion_sequence += 1
            self._aim_motion_pending = None
        self._set_aim_diagnostics(
            queue_state='EMPTY', queue_steps=0,
            output_state='CANCELLED', output_reason=str(reason)[:32],
            command_dx=0, command_dy=0,
        )
        self._aim_motion_wakeup.set()
        return True

    def _cancel_aim_motion(self, reason='CANCELLED', output_state='CANCELLED'):
        """Invalidate queued and in-flight micro-steps immediately."""
        with self._aim_motion_lock:
            self._aim_motion_sequence += 1
            self._aim_motion_pending = None
        self._set_aim_diagnostics(
            queue_state='EMPTY', queue_steps=0,
            output_state=output_state, output_reason=str(reason)[:32],
            command_dx=0, command_dy=0,
        )
        self._aim_motion_wakeup.set()

    def _stop_aim_motion_worker(self):
        self._cancel_aim_motion('RUNTIME STOPPED')
        self._aim_motion_stop.set()
        self._aim_motion_wakeup.set()
        if self._aim_motion_thread and self._aim_motion_thread.is_alive():
            self._aim_motion_thread.join(timeout=0.75)
        self._aim_motion_thread = None

    @staticmethod
    def _median_value(values):
        """Return a robust median for the recent mouse-response samples."""
        ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
        if not ordered:
            return None
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) * 0.5

    def _learn_axis_response(self, axis, sent_counts, observed_error_change):
        """Estimate signed screen-pixels per mouse-count from observed feedback."""
        sent_counts = float(sent_counts)
        observed_error_change = float(observed_error_change)
        if abs(sent_counts) < 1.25 or abs(observed_error_change) < 0.35:
            return False
        sample = -observed_error_change / sent_counts
        if not math.isfinite(sample) or not (0.012 <= abs(sample) <= 16.0):
            return False

        samples_attr = '_adaptive_gain_samples_' + axis
        gain_attr = '_adaptive_gain_' + axis
        samples = getattr(self, samples_attr)
        samples.append(sample)
        del samples[:-7]
        median = self._median_value(samples)
        if median is None or not (0.012 <= abs(median) <= 16.0):
            return False

        old_gain = float(getattr(self, gain_attr))
        if not math.isfinite(old_gain) or abs(old_gain) < 0.012:
            old_gain = 1.0

        # A large first observation is useful only if there was enough real
        # movement; otherwise wait for three consistent samples before learning.
        if len(samples) == 1:
            if abs(sent_counts) < 4.0 or abs(observed_error_change) < 2.0:
                return False
            # Learn magnitude quickly, but never reverse an axis from one sample.
            if median * old_gain <= 0.0:
                return False
            target_mag = max(abs(old_gain) * 0.20,
                             min(abs(old_gain) * 5.0, abs(median)))
            target = math.copysign(target_mag, median)
            setattr(self, gain_attr, old_gain + 0.45 * (target - old_gain))
            if len(samples) >= 3:
                setattr(self, '_adaptive_gain_ready_' + axis, True)
            return True

        if len(samples) < 3:
            return False
        same_sign = sum(1 for value in samples if value * median > 0.0)
        if same_sign < max(3, int(math.ceil(len(samples) * 0.70))):
            return False

        old_sign = 1.0 if old_gain >= 0.0 else -1.0
        target_sign = 1.0 if median >= 0.0 else -1.0
        # A sign change is allowed only after five mutually consistent samples
        # from broad, validated background flow. This supports inverted game axes
        # without ever learning direction from a moving target box.
        if old_sign != target_sign:
            if len(samples) >= 5 and same_sign >= 5:
                target_mag = max(0.05, min(16.0, abs(median)))
                setattr(self, gain_attr, target_sign * target_mag)
                setattr(self, '_adaptive_gain_ready_' + axis, True)
                samples[:] = samples[-3:]
                return True
            return False
        target_mag = max(0.012, min(16.0, abs(median)))
        old_mag = max(0.012, min(16.0, abs(old_gain)))
        target_mag = max(old_mag * 0.20, min(old_mag * 5.0, target_mag))
        new_gain = old_gain + 0.50 * (target_sign * target_mag - old_gain)
        setattr(self, gain_attr, new_gain)
        if len(samples) >= 3:
            setattr(self, '_adaptive_gain_ready_' + axis, True)
        return True

    @staticmethod
    def _estimate_camera_response_latency(output_history, flow_history, current_latency):
        """Estimate command-to-camera delay from one-to-one coherent flow pairs."""
        outputs = [
            (float(item[0]), int(item[1]), int(item[2]))
            for item in output_history
            if len(item) >= 3 and abs(int(item[1])) + abs(int(item[2])) >= 2
        ]
        flows = [
            (float(item[0]), float(item[1]), float(item[2]))
            for item in flow_history
            if len(item) >= 7
            and float(item[3]) >= 0.22
            and (int(item[4]) >= 16 or (int(item[4]) >= 3 and float(item[5]) >= 0.70))
            and float(item[6]) <= 1.45
        ]
        if len(outputs) < 4 or len(flows) < 4:
            return None

        best_lag = None
        best_score = -1.0
        best_residual = float('inf')
        tolerance = 0.018
        for lag_ms in range(0, 121, 5):
            lag = lag_ms / 1000.0
            scores = []
            residuals = []
            used = set()
            for out_stamp, out_x, out_y in outputs:
                expected = out_stamp + lag
                candidates = [
                    (abs(flows[index][0] - expected), index)
                    for index in range(len(flows)) if index not in used
                ]
                if not candidates:
                    break
                timing_residual, nearest_index = min(candidates)
                if timing_residual > tolerance:
                    continue
                _, flow_x, flow_y = flows[nearest_index]
                output_mag = math.hypot(out_x, out_y)
                flow_mag = math.hypot(flow_x, flow_y)
                if output_mag < 2.0 or flow_mag < 0.8:
                    continue
                # Scene flow is expected to oppose camera movement caused by output.
                alignment = -((out_x * flow_x) + (out_y * flow_y)) / max(
                    1e-6, output_mag * flow_mag
                )
                if math.isfinite(alignment):
                    scores.append(max(-1.0, min(1.0, alignment)))
                    residuals.append(timing_residual)
                    used.add(nearest_index)
            if len(scores) < 4:
                continue
            mean_score = sum(scores) / len(scores)
            confidence = mean_score * min(1.0, len(scores) / 8.0)
            mean_residual = sum(residuals) / max(1, len(residuals))
            if (
                confidence > best_score
                or (math.isclose(confidence, best_score, abs_tol=1e-6)
                    and mean_residual < best_residual)
            ):
                best_score = confidence
                best_residual = mean_residual
                best_lag = lag

        if best_lag is None or best_score < 0.38:
            return None
        current = max(0.015, min(0.120, float(current_latency)))
        return max(0.015, min(0.120, current + 0.22 * (best_lag - current)))

    def _adaptive_controller_feedback(self, error_x, error_y, now):
        """Calibrate mouse response from validated global background flow.

        Target-box motion is deliberately excluded: jumps, strafes and animations
        must never be mistaken for camera response.  A sample is accepted only
        when the mouse-output direction is coherent, the player is not moving the
        physical mouse/keyboard, and the global optical-flow fit has broad support.
        """
        track_id = getattr(self, '_aim_locked_track_id', None)
        latency = max(0.015, min(0.120, float(self._adaptive_latency_s)))
        with self._adaptive_feedback_lock:
            output_history = list(self._adaptive_output_history)
            flow_history = list(getattr(self, '_camera_flow_history', ()))
            cutoff = now - 0.65
            self._adaptive_output_history = [item for item in output_history if item[0] >= cutoff]
            self._camera_flow_history = [item for item in flow_history if item[0] >= cutoff]

        manual_dx = int(getattr(self, '_manual_mouse_dx', 0) or 0)
        manual_dy = int(getattr(self, '_manual_mouse_dy', 0) or 0)
        manual_last_at = float(getattr(self, '_manual_mouse_last_at', 0.0) or 0.0)
        manual_recent = manual_last_at > 0.0 and (now - manual_last_at) < 0.12
        raw_input_ready = bool(getattr(self, '_raw_mouse_input_available', False))
        manual_input = (
            abs(manual_dx) + abs(manual_dy) >= 1
            or manual_recent
            or not raw_input_ready
            or bool(self._is_player_moving())
        )
        if not manual_input:
            latency_estimate = self._estimate_camera_response_latency(
                output_history, flow_history, self._adaptive_latency_s
            )
            if latency_estimate is not None:
                self._adaptive_latency_s = latency_estimate

        latency = max(0.015, min(0.120, float(self._adaptive_latency_s)))
        for axis, current_error in (('x', float(error_x)), ('y', float(error_y))):
            error_attr = '_adaptive_prev_error_' + axis
            time_attr = '_adaptive_prev_observation_time_' + axis
            track_attr = '_adaptive_prev_track_id_' + axis
            previous_time = getattr(self, time_attr)
            previous_track = getattr(self, track_attr)

            if previous_time is None or previous_track != track_id or track_id is None:
                setattr(self, error_attr, current_error)
                setattr(self, time_attr, now)
                setattr(self, track_attr, track_id)
                continue

            elapsed_raw = max(0.0, now - float(previous_time))
            if elapsed_raw > 0.22:
                setattr(self, error_attr, current_error)
                setattr(self, time_attr, now)
                continue

            if manual_input:
                setattr(self, error_attr, current_error)
                setattr(self, time_attr, now)
                continue

            # Mouse commands are observed after the game's camera latency; scene
            # flow is measured on the corresponding later captured frames.
            output_start = float(previous_time) - latency
            output_end = float(now) - latency
            outputs = [item for item in output_history if output_start < item[0] <= output_end]
            flows = [
                item for item in flow_history
                if float(previous_time) < item[0] <= now
                and (
                    (item[3] >= 0.54 and item[4] >= 16 and item[5] >= 0.024)
                    or (item[3] >= 0.22 and item[4] >= 3 and item[5] >= 0.70)
                )
                and item[6] <= 1.45
            ]

            if axis == 'x':
                sent_values = [int(item[1]) for item in outputs]
                flow_values = [float(item[1]) for item in flows]
            else:
                sent_values = [int(item[2]) for item in outputs]
                flow_values = [float(item[2]) for item in flows]

            sent_counts = float(sum(sent_values))
            sent_abs = float(sum(abs(value) for value in sent_values))
            flow_delta = float(sum(flow_values))
            flow_abs = float(sum(abs(value) for value in flow_values))
            output_coherence = abs(sent_counts) / max(1.0, sent_abs)
            flow_coherence = abs(flow_delta) / max(1.0, flow_abs)

            # Background moves opposite to the camera/mouse direction. Require
            # several counts, a measured scene shift, coherent directions, and
            # high-support flow before changing the pixels-per-count estimate.
            sample_ready = (
                len(flows) >= 1
                and abs(sent_counts) >= 3.0
                and sent_abs >= 3.0
                and output_coherence >= 0.68
                and abs(flow_delta) >= 1.25
                and flow_coherence >= 0.62
            )
            learned = False
            if sample_ready:
                pixels_per_count = abs(flow_delta / sent_counts)
                if math.isfinite(pixels_per_count) and 0.05 <= pixels_per_count <= 16.0:
                    # _learn_axis_response derives the signed gain; five strong
                    # same-sign samples are required before accepting an inverted axis.
                    learned = self._learn_axis_response(axis, sent_counts, flow_delta)

            # Advance the baseline every 100 ms even if the sample is rejected;
            # never combine unrelated movement bursts into one calibration value.
            if learned or elapsed_raw >= 0.10 or sample_ready:
                setattr(self, error_attr, current_error)
                setattr(self, time_attr, now)

        return float(error_x), float(error_y)

    @staticmethod
    def _tracking_response_profile(speed, strength, dt, calibrated=True):
        """Return a frame-rate-aware control profile for the two independent sliders.

        Speed controls response bandwidth and the per-second output budget. Force
        controls correction magnitude. The calculation is time-based so 15 FPS and
        60 FPS do not interpret the same slider value as radically different rates.
        """
        speed = max(0.10, min(1.00, float(speed)))
        strength = max(0.10, min(1.00, float(strength)))
        dt = max(1.0 / 240.0, min(0.10, float(dt)))

        speed_norm = (speed - 0.10) / 0.90
        force_norm = (strength - 0.10) / 0.90

        # The previous 65..300 /s mapping saturated at ordinary frame intervals:
        # even its minimum yielded >98% response at 15.5 FPS. This 4..42 /s curve
        # keeps low/mid/high speed distinguishable at the observed 64 ms cadence.
        response_rate = 4.0 + 38.0 * (speed_norm ** 1.10)
        approach_base = -math.expm1(-response_rate * dt)

        # Keep Force meaningful without allowing its low end to collapse the
        # controller to one tenth of the requested movement.
        force_gain = 0.40 + 0.60 * (force_norm ** 1.20)

        # Express the output ceiling as counts/second, then convert to this frame's
        # budget. This removes the old 24-count uncalibrated ceiling that made most
        # of the Speed slider ineffective while camera-flow calibration was pending.
        counts_per_second = 220.0 + 1050.0 * (speed_norm ** 1.15)
        max_counts = max(1, min(96, int(round(counts_per_second * dt))))
        if not calibrated:
            # A modest safety margin remains until the game's sensitivity is learned,
            # but calibration no longer clamps every speed value to 24 counts.
            max_counts = max(1, int(round(max_counts * 0.90)))

        return {
            'speed_norm': speed_norm,
            'force_norm': force_norm,
            'response_rate': response_rate,
            'approach_base': max(0.0, min(0.985, approach_base)),
            'force_gain': force_gain,
            'max_counts': max_counts,
            'counts_per_second': counts_per_second,
        }

    def _apply_aim_assist_stable(self, target_x, target_y, tracking_weight=1.0):
        """Strong time-based tracking controller with independent Force/Speed.

        Tracking Speed (0.10..1.00) controls the response rate. Aim Force
        (0.10..1.00) controls the fraction of the current error pulled toward
        the selected point. Both use a deliberately strong nonlinear mapping:
        0.10 is still responsive, while 1.00 approaches the full error each frame.
        """
        speed = max(0.10, min(1.00, float(
            getattr(self, 'aim_speed', getattr(self, 'aim_smooth', 0.75))
        )))
        strength = max(0.10, min(1.00, float(getattr(self, 'aim_strength', 0.75))))
        tracking_weight = max(0.0, min(1.0, float(tracking_weight)))
        if speed <= 0.0 or strength <= 0.0 or tracking_weight <= 0.0:
            reason = 'SPEED ZERO' if speed <= 0.0 else ('STRENGTH ZERO' if strength <= 0.0 else 'TRACK WEIGHT ZERO')
            self._aim_remainder_x = self._aim_remainder_y = 0.0
            self._stable_prev_error_x = self._stable_prev_error_y = None
            self._stable_inside_deadzone = False
            self._aim_last_update_time = None
            self._set_aim_diagnostics(decision='BLOCKED', reason=reason,
                                      tracking_weight=tracking_weight,
                                      command_dx=0, command_dy=0)
            self._cancel_aim_motion(reason)
            return

        now = time.perf_counter()
        warmup_until = float(getattr(self, '_aim_activation_warmup_until', 0.0) or 0.0)
        if now < warmup_until:
            self._aim_remainder_x = self._aim_remainder_y = 0.0
            self._aim_last_update_time = None
            self._stable_prev_error_x = self._stable_prev_error_y = None
            self._reset_new_controller_state()
            self._cancel_aim_motion('ACTIVATION SETTLE', output_state='IDLE')
            self._set_aim_diagnostics(
                decision='HOLD', reason='ACTIVATION SETTLE',
                target_id=getattr(self, '_aim_locked_track_id', None),
                tracking_weight=0.0, command_dx=0, command_dy=0,
            )
            return

        previous_time = self._aim_last_update_time
        dt = 1.0 / 60.0 if previous_time is None else max(
            1.0 / 240.0, min(0.10, now - previous_time)
        )
        self._aim_last_update_time = now

        # Predict only one short control horizon from the measured, filtered
        # target velocity. This compensates capture/inference/input latency while
        # keeping the aim point inside the selected hitbox and active FOV.
        measured_x = float(target_x)
        measured_y = float(target_y)
        target_vx = max(-2200.0, min(2200.0, float(getattr(self, '_aim_point_velocity_x', 0.0) or 0.0)))
        target_vy = max(-2200.0, min(2200.0, float(getattr(self, '_aim_point_velocity_y', 0.0) or 0.0)))
        measurement_stamp = getattr(self, '_aim_last_valid_measurement_time', None)
        measurement_age = (
            max(0.0, min(0.080, now - float(measurement_stamp)))
            if measurement_stamp is not None else 0.0
        )
        measured_camera_latency = max(
            0.015, min(0.120, float(getattr(self, '_adaptive_latency_s', 0.050)))
        )
        prediction_horizon = min(0.090, measurement_age + measured_camera_latency)
        hitbox = getattr(self, '_aim_locked_hitbox', None)
        if hitbox is not None:
            hitbox_w = max(1.0, float(hitbox[2]) - float(hitbox[0]))
            hitbox_h = max(1.0, float(hitbox[3]) - float(hitbox[1]))
            lead_cap_x = max(1.0, min(18.0, hitbox_w * 0.18))
            lead_cap_y = max(1.0, min(18.0, hitbox_h * 0.18))
        else:
            lead_cap_x = lead_cap_y = 8.0
        predicted_x = measured_x + max(-lead_cap_x, min(lead_cap_x, target_vx * prediction_horizon))
        predicted_y = measured_y + max(-lead_cap_y, min(lead_cap_y, target_vy * prediction_horizon))
        if hitbox is not None:
            predicted_x = max(float(hitbox[0]), min(float(hitbox[2]), predicted_x))
            predicted_y = max(float(hitbox[1]), min(float(hitbox[3]), predicted_y))
        if self._aim_point_inside_fov(predicted_x, predicted_y):
            control_target_x, control_target_y = predicted_x, predicted_y
        else:
            control_target_x, control_target_y = measured_x, measured_y
        error_x = control_target_x - float(self.crosshair_x)
        error_y = control_target_y - float(self.crosshair_y)
        raw_distance = math.hypot(error_x, error_y)
        self._last_movement_input = self._is_player_moving()

        # Learn the actual screen-pixels moved by one mouse count. This prevents
        # treating mouse counts as pixels on every in-game sensitivity.
        effective_x, effective_y = self._adaptive_controller_feedback(
            error_x, error_y, now
        )
        control_distance = math.hypot(effective_x, effective_y)

        # Hysteresis uses only the newest observed frame error.
        enter_radius = 0.8
        exit_radius = 1.6
        if self._stable_inside_deadzone:
            if control_distance <= exit_radius:
                self._aim_remainder_x = self._aim_remainder_y = 0.0
                self._stable_prev_error_x, self._stable_prev_error_y = effective_x, effective_y
                self._aim_previous_error_x = effective_x
                self._aim_previous_error_y = effective_y
                self._set_aim_diagnostics(decision='HOLD', reason='AT TARGET',
                                          error_x=error_x, error_y=error_y,
                                          distance=raw_distance, approach=0.0,
                                          tracking_weight=tracking_weight)
                self._cancel_aim_motion('AT TARGET', output_state='IDLE')
                return
            self._stable_inside_deadzone = False
        elif control_distance <= enter_radius:
            self._stable_inside_deadzone = True
            self._aim_remainder_x = self._aim_remainder_y = 0.0
            self._stable_prev_error_x, self._stable_prev_error_y = effective_x, effective_y
            self._aim_previous_error_x = effective_x
            self._aim_previous_error_y = effective_y
            self._set_aim_diagnostics(decision='HOLD', reason='AT TARGET',
                                      error_x=error_x, error_y=error_y,
                                      distance=raw_distance, approach=0.0,
                                      tracking_weight=tracking_weight)
            self._cancel_aim_motion('AT TARGET', output_state='IDLE')
            return

        previous_axis_x = self._aim_previous_error_x
        previous_axis_y = self._aim_previous_error_y
        target_velocity_x = float(getattr(self, '_aim_point_velocity_x', 0.0) or 0.0)
        target_velocity_y = float(getattr(self, '_aim_point_velocity_y', 0.0) or 0.0)
        if not math.isfinite(target_velocity_x):
            target_velocity_x = 0.0
        if not math.isfinite(target_velocity_y):
            target_velocity_y = 0.0
        target_crossed_x = self._target_motion_explains_crossing(
            previous_axis_x, effective_x, target_velocity_x, dt
        )
        target_crossed_y = self._target_motion_explains_crossing(
            previous_axis_y, effective_y, target_velocity_y, dt
        )
        axis_gain_x = self._update_axis_gain('x', effective_x, target_crossed_x)
        axis_gain_y = self._update_axis_gain('y', effective_y, target_crossed_y)

        def approach_brake(previous, current, target_crossed):
            if previous is None or not math.isfinite(float(previous)):
                return 1.0
            previous = float(previous)
            current = float(current)
            if previous * current <= 0.0:
                # Do not brake when the target itself moved across the reticle.
                # Keep braking for a crossing caused by the prior mouse correction.
                return 1.0 if target_crossed else 0.48
            before = abs(previous)
            after = abs(current)
            if after < before:
                closing = min(1.0, (before - after) / max(2.0, before))
                return max(0.52, 1.0 - 0.48 * closing)
            return 1.0

        brake_x = approach_brake(previous_axis_x, effective_x, target_crossed_x)
        brake_y = approach_brake(previous_axis_y, effective_y, target_crossed_y)
        self._last_aim_gain_factor = (axis_gain_x * brake_x + axis_gain_y * brake_y) * 0.5

        previous_x, previous_y = self._stable_prev_error_x, self._stable_prev_error_y
        if previous_x is not None and previous_x * effective_x < 0.0:
            self._aim_remainder_x = 0.0
        if previous_y is not None and previous_y * effective_y < 0.0:
            self._aim_remainder_y = 0.0
        self._stable_prev_error_x, self._stable_prev_error_y = effective_x, effective_y

        # Speed and Force have separate, frame-rate-aware roles. At the observed
        # ~15.5 FPS cadence, the former 65..300 /s response curve was already
        # saturated at its slowest setting; Strength therefore appeared to be the
        # only slider that changed tracking. Use one shared, testable profile.
        calibrated_x = bool(getattr(self, '_adaptive_gain_ready_x', False))
        calibrated_y = bool(getattr(self, '_adaptive_gain_ready_y', False))
        profile = self._tracking_response_profile(
            speed, strength, dt, calibrated=(calibrated_x and calibrated_y)
        )
        response_rate = profile['response_rate']
        approach_base = profile['approach_base']
        force_gain = profile['force_gain']

        # Force controls correction magnitude; Speed independently controls how
        # quickly each fresh frame is allowed to close the measured error.
        approach = max(0.0, min(0.985, approach_base * force_gain * tracking_weight))

        # Convert screen-pixel error into mouse counts using the learned game
        # response. Keep the sign so inverted axes remain supported. Clamp the
        # estimate to a sane finite range until enough feedback samples exist.
        pixels_per_count_x = float(getattr(self, '_adaptive_gain_x', 1.0))
        pixels_per_count_y = float(getattr(self, '_adaptive_gain_y', 1.0))
        if not math.isfinite(pixels_per_count_x) or abs(pixels_per_count_x) < 0.05:
            pixels_per_count_x = 3.0
        if not math.isfinite(pixels_per_count_y) or abs(pixels_per_count_y) < 0.05:
            pixels_per_count_y = 3.0
        pixels_per_count_x = math.copysign(max(0.05, min(16.0, abs(pixels_per_count_x))), pixels_per_count_x)
        pixels_per_count_y = math.copysign(max(0.05, min(16.0, abs(pixels_per_count_y))), pixels_per_count_y)
        desired_x = (effective_x / pixels_per_count_x) * approach * axis_gain_x * brake_x
        desired_y = (effective_y / pixels_per_count_y) * approach * axis_gain_y * brake_y

        # The output ceiling scales with the time represented by this frame and
        # Tracking Speed. It remains bounded while avoiding the former hard 24-count
        # ceiling whenever optical-flow calibration had not yet become ready.
        max_counts = profile['max_counts']
        desired_x = max(-max_counts, min(max_counts, desired_x))
        desired_y = max(-max_counts, min(max_counts, desired_y))
        raw_x = desired_x + self._aim_remainder_x
        raw_y = desired_y + self._aim_remainder_y
        move_x = int(math.trunc(raw_x))
        move_y = int(math.trunc(raw_y))
        self._aim_remainder_x = raw_x - move_x
        self._aim_remainder_y = raw_y - move_y

        self._set_aim_diagnostics(
            decision='ALLOW', reason='TARGET VALID',
            target_id=getattr(self, '_aim_locked_track_id', None),
            error_x=error_x, error_y=error_y, distance=raw_distance,
            response_rate=response_rate, approach=approach,
            tracking_weight=tracking_weight,
            speed_cap_counts=max_counts,
            gain_calibrated=bool(calibrated_x and calibrated_y),
            force_gain=force_gain,
            pixels_per_count_x=pixels_per_count_x, pixels_per_count_y=pixels_per_count_y,
            brake_x=brake_x, brake_y=brake_y,
            command_dx=move_x, command_dy=move_y,
        )
        if move_x or move_y:
            duration = max(0.010, min(0.022, dt * 0.55))
            self._submit_aim_motion(
                move_x, move_y, duration=duration,
                target_id=getattr(self, '_aim_locked_track_id', None),
            )
        else:
            self._set_aim_diagnostics(queue_state='EMPTY', queue_steps=0,
                                      output_state='IDLE', output_reason='SUBPIXEL')

    def _apply_aim_assist_old(self, target_x, target_y, tracking_weight=1.0):
        """Compatibility entry point; now uses the stable shared controller."""
        self._apply_aim_assist_stable(target_x, target_y, tracking_weight)

    def _apply_aim_assist_new(self, target_x, target_y, tracking_weight=1.0):
        """Compatibility entry point; now uses the stable shared controller."""
        self._apply_aim_assist_stable(target_x, target_y, tracking_weight)

    def _apply_aim_assist(self, target_x, target_y, tracking_weight=1.0):
        """Keep OLD/NEW settings backward-compatible with one stable output law."""
        if getattr(self, 'aim_method', 'old') == 'new':
            self._apply_aim_assist_new(target_x, target_y, tracking_weight)
        else:
            self._apply_aim_assist_old(target_x, target_y, tracking_weight)

    @staticmethod
    def update_status_aimbot():
        instance = Aimbot.active_instance
        active = bool(instance and instance.master_enabled and instance.aim_enabled)
        state = 'ENABLED' if active else 'DISABLED'
        print(f'[INFO] Aim Assist [{state}]')

    @staticmethod
    def is_aimbot_enabled():
        instance = Aimbot.active_instance
        return bool(instance and instance.master_enabled and instance.aim_enabled)

    def set_left_button(self, pressed):
        pressed = bool(pressed)
        if pressed == self.trigger_held:
            return
        match self.mouse_method.lower():
            case 'ddxoft':
                Aimbot.mouse_dll.DD_btn(1 if pressed else 2)
            case 'win32':
                flag = 0x0002 if pressed else 0x0004
                ctypes.windll.user32.mouse_event(flag, 0, 0, 0, 0)
            case 'makcu':
                raise NotImplementedError(
                    'Hold mode is not defined by this project MAKCU UDP protocol. '
                    'Set mouse_method = \'win32\' or \'ddxoft\'.'
                )
        self.trigger_held = pressed

    def _load_crosshair_position(self):
        try:
            with open(self.crosshair_position_path, 'r', encoding='utf-8') as file:
                data = json.load(file)
            if 'calibration_offset_x' in data and 'calibration_offset_y' in data:
                self._calibrated_reticle_offset_x = float(data.get('calibration_offset_x', 0.0))
                self._calibrated_reticle_offset_y = float(data.get('calibration_offset_y', 0.0))
            else:
                saved_width = max(1, int(data.get('box_width', data.get('box_size', self.capture_width))))
                saved_height = max(1, int(data.get('box_height', data.get('box_size', self.capture_height))))
                saved_center_x = (saved_width - 1) / 2.0
                saved_center_y = (saved_height - 1) / 2.0
                if 'x_norm' in data and 'y_norm' in data:
                    saved_x = float(data['x_norm']) * max(0, saved_width - 1)
                    saved_y = float(data['y_norm']) * max(0, saved_height - 1)
                else:
                    saved_x = float(data.get('x', saved_center_x))
                    saved_y = float(data.get('y', saved_center_y))
                self._calibrated_reticle_offset_x = saved_x - saved_center_x
                self._calibrated_reticle_offset_y = saved_y - saved_center_y
            current_center_x = (self.capture_width - 1) / 2.0
            current_center_y = (self.capture_height - 1) / 2.0
            self.crosshair_x = current_center_x + self._calibrated_reticle_offset_x
            self.crosshair_y = current_center_y + self._calibrated_reticle_offset_y
            print(
                f'[INFO] Loaded Debug reticle calibration offset: '
                f'({self._calibrated_reticle_offset_x:+.1f}, {self._calibrated_reticle_offset_y:+.1f})px'
            )
        except FileNotFoundError:
            print('[INFO] No saved debug crosshair position; using crop center.')
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f'[WARN] Could not load debug crosshair position: {exc}')

    def _save_crosshair_position(self):
        center_x = (self.capture_width - 1) / 2.0
        center_y = (self.capture_height - 1) / 2.0
        # When the crop is centered between the reticle and displaced FOV, the
        # reticle's offset from crop center includes half of the FOV displacement.
        if self._current_detection_box is not None and self._capture_bounds is not None:
            bounds_left, bounds_top, bounds_right, bounds_bottom = map(int, self._capture_bounds)
            monitor_center_x = (bounds_left + bounds_right) * 0.5
            monitor_center_y = (bounds_top + bounds_bottom) * 0.5
            calibration_x = (float(self._current_detection_box['left']) + float(self.crosshair_x)) - monitor_center_x
            calibration_y = (float(self._current_detection_box['top']) + float(self.crosshair_y)) - monitor_center_y
        else:
            calibration_x = float(self.crosshair_x) - center_x + float(self.aim_fov_offset_x) * 0.5
            calibration_y = float(self.crosshair_y) - center_y + float(self.aim_fov_offset_y) * 0.5
        self._calibrated_reticle_offset_x = calibration_x
        self._calibrated_reticle_offset_y = calibration_y
        max_x = max(1, self.capture_width - 1)
        max_y = max(1, self.capture_height - 1)
        data = {
            'x_norm': round((center_x + calibration_x) / max_x, 8),
            'y_norm': round((center_y + calibration_y) / max_y, 8),
            'box_width': int(self.capture_width),
            'box_height': int(self.capture_height),
            'box_size': int(max(self.capture_width, self.capture_height)),
            'calibration_offset_x': round(calibration_x, 4),
            'calibration_offset_y': round(calibration_y, 4),
        }
        try:
            os.makedirs(os.path.dirname(self.crosshair_position_path), exist_ok=True)
            temp_path = self.crosshair_position_path + '.tmp'
            with open(temp_path, 'w', encoding='utf-8') as file:
                json.dump(data, file, indent=2)
            os.replace(temp_path, self.crosshair_position_path)
        except OSError as exc:
            print(f'[WARN] Could not save debug crosshair position: {exc}')

    def update_debug_crosshair_position(self):
        # Skip every calibration-related Windows call while Debug is hidden.
        if not (self.debug_mode or self.debug_record_mode):
            return
        # Move the debug aim marker and persist it; only when a Debug window is focused.
        now = time.perf_counter()
        user32 = ctypes.windll.user32
        find_window = user32.FindWindowW
        find_window.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        find_window.restype = wintypes.HWND
        get_foreground = user32.GetForegroundWindow
        get_foreground.restype = wintypes.HWND
        debug_hwnd = find_window(None, self.debug_window_name)
        record_hwnd = find_window(None, self.debug_record_window_name)
        foreground = get_foreground()
        debug_is_foreground = (
            (bool(debug_hwnd) and foreground == debug_hwnd)
            or (bool(record_hwnd) and foreground == record_hwnd)
        )

        # Numpad controls work with NumLock ON; arrows work with NumLock OFF.
        directions = {
            'left':  (0x64, 0x25),  # Numpad 4 / Left
            'right': (0x66, 0x27),  # Numpad 6 / Right
            'up':    (0x68, 0x26),  # Numpad 8 / Up
            'down':  (0x28,),       # Down arrow; Numpad 2 is reserved for recording toggle
        }
        shift_down = bool(win32api.GetAsyncKeyState(0x10) & 0x8000)
        step = 10 if shift_down else 1
        moved = False

        for direction, virtual_keys in directions.items():
            down = any(bool(win32api.GetAsyncKeyState(vk) & 0x8000) for vk in virtual_keys)
            was_down = self._debug_direction_down[direction]
            due_for_repeat = now >= self._debug_direction_repeat_at[direction]
            if self.debug_mode and debug_is_foreground and down and (not was_down or due_for_repeat):
                if direction == 'left':
                    self.crosshair_x -= step
                elif direction == 'right':
                    self.crosshair_x += step
                elif direction == 'up':
                    self.crosshair_y -= step
                elif direction == 'down':
                    self.crosshair_y += step
                self._debug_direction_repeat_at[direction] = now + 0.065
                moved = True
            elif not down:
                self._debug_direction_repeat_at[direction] = 0.0
            self._debug_direction_down[direction] = down

        if moved:
            max_x = max(0, self.capture_width - 1)
            max_y = max(0, self.capture_height - 1)
            self.crosshair_x = min(max_x, max(0.0, self.crosshair_x))
            self.crosshair_y = min(max_y, max(0.0, self.crosshair_y))
            self._save_crosshair_position()

        # Numpad 5 recenters the marker (Clear key when NumLock is OFF).
        reset_down = any(
            bool(win32api.GetAsyncKeyState(vk) & 0x8000)
            for vk in (0x65, 0x0C)
        )
        if self.debug_mode and debug_is_foreground and reset_down and not self._debug_reset_was_down:
            active_offset_x, active_offset_y = self._active_fov_offset()
            self.crosshair_x = (self.capture_width - 1) / 2.0 - active_offset_x * 0.5
            self.crosshair_y = (self.capture_height - 1) / 2.0 - active_offset_y * 0.5
            self._save_crosshair_position()
            print('[INFO] Debug crosshair reset to crop center and saved.')
        self._debug_reset_was_down = reset_down

    def _load_aim_point_calibration(self):
        try:
            with open(self.aim_point_config_path, 'r', encoding='utf-8') as file:
                data = json.load(file)
            self.aim_point_u = max(0.0, min(1.0, float(data.get('u', 0.50))))
            self.aim_point_v = max(0.0, min(1.0, float(data.get('v', 0.09))))
            print(
                f'[INFO] Loaded custom Aim Assist point: '
                f'X={self.aim_point_u:.3f}, Y={self.aim_point_v:.3f}',
                flush=True,
            )
        except FileNotFoundError:
            print('[INFO] No custom Aim Assist point saved; using head-center default.', flush=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f'[WARN] Could not load custom Aim Assist point: {exc}', flush=True)

    def _save_aim_point_calibration(self):
        data = {
            'u': round(float(self.aim_point_u), 6),
            'v': round(float(self.aim_point_v), 6),
        }
        try:
            os.makedirs(os.path.dirname(self.aim_point_config_path), exist_ok=True)
            temp_path = self.aim_point_config_path + '.tmp'
            with open(temp_path, 'w', encoding='utf-8') as file:
                json.dump(data, file, indent=2)
            os.replace(temp_path, self.aim_point_config_path)
            print(
                f'[INFO] Custom Aim Assist point saved: '
                f'X={self.aim_point_u:.3f}, Y={self.aim_point_v:.3f}',
                flush=True,
            )
        except OSError as exc:
            print(f'[WARN] Could not save custom Aim Assist point: {exc}', flush=True)

    def _sha256_file(path):
        digest = hashlib.sha256()
        with open(path, 'rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    def _load_head_detector(self):
        """Load the verified v8s-head checkpoint, downloading it when absent.

        The checkpoint is kept separate from lib/best.pt. If download, hash
        verification, or model loading fails, the original custom Aim Point
        remains available as a safe fallback and the runtime still starts.
        """
        expected_sha256 = '1467b10e30a05be6a05950190e5efe30b50681270cb7baf6115ec96806b25964'
        repo_id = 'wesjos/YOLO-head-detection'
        filename = 'v8s-head.pt'
        os.makedirs(self.lib_dir, exist_ok=True)

        try:
            if not os.path.isfile(self.head_model_path):
                print('[INFO] Head detector weights missing; downloading v8s-head.pt from Hugging Face...', flush=True)
                downloaded_path = None
                try:
                    from huggingface_hub import hf_hub_download
                    downloaded_path = hf_hub_download(
                        repo_id=repo_id,
                        filename=filename,
                        local_dir=self.lib_dir,
                    )
                except Exception as hub_exc:
                    # Standard-library fallback for environments where the
                    # huggingface_hub helper is unavailable.
                    try:
                        import urllib.request
                        temp_path = self.head_model_path + '.download'
                        request = urllib.request.Request(
                            'https://huggingface.co/wesjos/YOLO-head-detection/resolve/main/v8s-head.pt',
                            headers={'User-Agent': 'Lunar-HeadDetector/1.0'},
                        )
                        with urllib.request.urlopen(request, timeout=90) as response, open(temp_path, 'wb') as output:
                            shutil.copyfileobj(response, output)
                        downloaded_path = temp_path
                    except Exception as url_exc:
                        raise RuntimeError(
                            f'Hugging Face download failed ({hub_exc}); direct download failed ({url_exc})'
                        ) from url_exc

                if downloaded_path and os.path.abspath(downloaded_path) != os.path.abspath(self.head_model_path):
                    if os.path.isfile(downloaded_path):
                        if str(downloaded_path).endswith('.download'):
                            os.replace(downloaded_path, self.head_model_path)
                        else:
                            shutil.copy2(downloaded_path, self.head_model_path)

            if not os.path.isfile(self.head_model_path):
                raise FileNotFoundError(self.head_model_path)

            actual_sha256 = self._sha256_file(self.head_model_path)
            if actual_sha256.lower() != expected_sha256:
                raise RuntimeError(
                    'SHA-256 mismatch for v8s-head.pt. Download the official file again; '
                    f'expected {expected_sha256}, received {actual_sha256}.'
                )

            print('[INFO] Loading verified v8s-head.pt (single class: head)...', flush=True)
            self.head_model = YOLO(self.head_model_path)
            # The main character detector stays on CUDA. The auxiliary head
            # model is deliberately resident and executed on CPU to avoid a
            # second neural network competing for GTX 970 VRAM.
            try:
                inner = getattr(self.head_model, 'model', None)
                if inner is not None and hasattr(inner, 'to'):
                    inner.to('cpu')
            except Exception:
                pass
            self.head_model_ready = True
            print('[OK] Head detector ready on CPU worker: lib/v8s-head.pt', flush=True)
        except Exception as exc:
            self.head_model = None
            self.head_model_ready = False
            print(
                '[WARN] Head detector unavailable; using the existing calibrated Aim Point. '
                f'Details: {exc}',
                flush=True,
            )

    def _cached_head_points_for_records(self, records, now):
        """Reproject cached normalized head anchors onto current tracked boxes."""
        results = {}
        ages = []
        for record in records:
            track_id = record.get('track_id')
            cache = self._head_point_cache.get(track_id) if track_id is not None else None
            if not cache:
                continue
            age = max(0.0, float(now) - float(cache.get('last_detection', 0.0)))
            if age > 0.30:
                continue
            x1, y1, x2, y2 = map(float, record['box'])
            bw = max(2.0, x2 - x1)
            bh = max(4.0, y2 - y1)
            norm_x = float(cache.get('u', 0.5))
            norm_y = float(cache.get('v', 0.145))
            raw_u = float(cache.get('raw_u', norm_x))
            raw_v = float(cache.get('raw_v', norm_y))
            results[record['index']] = {
                'point': (x1 + norm_x * bw, y1 + norm_y * bh),
                'raw_point': (x1 + raw_u * bw, y1 + raw_v * bh),
                'box': None,
                'confidence': float(cache.get('confidence', 0.0)),
                'source': 'HEAD TRACK',
                'norm': (norm_x, norm_y),
                'head_size': (
                    float(cache.get('head_w_rel', 0.0)),
                    float(cache.get('head_h_rel', 0.0)),
                ),
            }
            ages.append(age)
        self._head_cache_age_ms = (max(ages) * 1000.0) if ages else 0.0
        return results

    def _start_head_inference_worker(self):
        """Start the one-slot, below-normal-priority CPU head-refinement worker."""
        thread = self._head_worker_thread
        if thread is not None and thread.is_alive():
            return
        self._head_worker_stop.clear()
        self._head_worker_wakeup.clear()
        self._head_worker_thread = threading.Thread(
            target=self._head_inference_worker_loop,
            name='LunarHeadRefineCPU',
            daemon=True,
        )
        self._head_worker_thread.start()

    def _stop_head_inference_worker(self):
        """Stop the auxiliary CPU worker without delaying runtime shutdown."""
        self._head_worker_stop.set()
        self._head_worker_wakeup.set()
        with self._head_worker_lock:
            self._head_worker_pending = None
        thread = self._head_worker_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
        self._head_worker_thread = None

    def _head_inference_worker_loop(self):
        """Run head-only inference on CPU, isolated from the main CUDA loop."""
        try:
            try:
                kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel32.GetCurrentThread.restype = wintypes.HANDLE
                kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
                kernel32.SetThreadPriority.restype = wintypes.BOOL
                kernel32.SetThreadPriority(kernel32.GetCurrentThread(), -1)  # BELOW_NORMAL
            except Exception:
                pass

            while not self._head_worker_stop.is_set():
                self._head_worker_wakeup.wait(0.20)
                self._head_worker_wakeup.clear()
                if self._head_worker_stop.is_set():
                    break

                while not self._head_worker_stop.is_set():
                    with self._head_worker_lock:
                        job = self._head_worker_pending
                        self._head_worker_pending = None
                        self._head_worker_busy = job is not None
                    if job is None:
                        break

                    started = time.perf_counter()
                    try:
                        items = job.get('items', [])
                        if not items or self.head_model is None:
                            continue
                        # Two small ROIs allow the current lock and the next
                        # nearest candidate to receive head refinement in one batch.
                        items = items[:2]
                        predictions = self.head_model.predict(
                            source=[item['roi'] for item in items],
                            imgsz=self.head_model_imgsz,
                            conf=self.head_model_conf,
                            iou=0.45,
                            max_det=8,
                            batch=len(items),
                            verbose=False,
                            device='cpu',
                        )
                        detections = {}
                        for item_index, item in enumerate(items):
                            if item_index >= len(predictions):
                                continue
                            track_id = item.get('track_id')
                            if track_id is None:
                                continue
                            x1, y1, x2, y2 = item['box']
                            bw = max(2.0, x2 - x1)
                            bh = max(4.0, y2 - y1)
                            pred_boxes = getattr(predictions[item_index], 'boxes', None)
                            if pred_boxes is None or len(pred_boxes) == 0:
                                continue
                            xyxy = pred_boxes.xyxy.detach().cpu().numpy()
                            confs = pred_boxes.conf.detach().cpu().numpy()
                            candidates = []
                            for coords, confidence_value in zip(xyxy, confs):
                                hx1, hy1, hx2, hy2 = map(float, coords[:4])
                                head_w = max(1.0, hx2 - hx1)
                                head_h = max(1.0, hy2 - hy1)
                                center_x = item['left'] + (hx1 + hx2) * 0.5
                                center_y = item['top'] + (hy1 + hy2) * 0.5
                                norm_x = (center_x - x1) / bw
                                norm_y = (center_y - y1) / bh
                                norm_head_w = head_w / bw
                                norm_head_h = head_h / bh
                                aspect = head_w / head_h

                                # Broad geometry tolerances work across different
                                # camera FOVs, character proportions and animations.
                                # Confidence remains the dominant signal; geometry
                                # only rejects boxes that cannot reasonably be heads.
                                if not (-0.08 <= norm_x <= 1.08):
                                    continue
                                if not (-0.08 <= norm_y <= 0.48):
                                    continue
                                if not (0.018 <= norm_head_w <= 0.78):
                                    continue
                                if not (0.018 <= norm_head_h <= 0.54):
                                    continue
                                if not (0.42 <= aspect <= 2.65):
                                    continue

                                conf_value = float(confidence_value)
                                center_penalty = min(1.0, abs(norm_x - 0.5) / 0.5)
                                vertical_penalty = min(1.0, abs(norm_y - 0.16) / 0.40)
                                size_penalty = min(1.0, abs(math.log(max(1e-4, norm_head_h) / 0.14)))
                                shape_penalty = min(1.0, abs(math.log(max(1e-4, aspect) / 1.0)))
                                score = (
                                    conf_value
                                    - 0.10 * center_penalty
                                    - 0.07 * vertical_penalty
                                    - 0.025 * size_penalty
                                    - 0.025 * shape_penalty
                                )
                                candidates.append((
                                    score, conf_value, norm_x, norm_y,
                                    norm_head_w, norm_head_h, aspect,
                                ))
                            if candidates:
                                candidates.sort(key=lambda value: value[0], reverse=True)
                                _, conf_value, norm_x, norm_y, norm_head_w, norm_head_h, aspect = candidates[0]
                                detections[int(track_id)] = {
                                    'raw_u': float(norm_x),
                                    'raw_v': float(norm_y),
                                    'head_w_rel': float(norm_head_w),
                                    'head_h_rel': float(norm_head_h),
                                    'head_aspect': float(aspect),
                                    'confidence': float(conf_value),
                                }

                        completed_at = time.perf_counter()
                        duration_ms = (completed_at - started) * 1000.0
                        with self._head_worker_lock:
                            self._head_worker_next_job_id += 1
                            self._head_worker_latest = {
                                'job_id': self._head_worker_next_job_id,
                                'completed_at': completed_at,
                                'duration_ms': duration_ms,
                                'detections': detections,
                            }
                            self._head_worker_last_ms = duration_ms
                            self._head_worker_last_error = ''
                    except Exception as exc:
                        with self._head_worker_lock:
                            self._head_worker_last_error = str(exc)[:180]
                        if not self._head_model_error_logged:
                            print(f'[WARN] CPU head refinement failed; calibrated body point remains available: {exc}', flush=True)
                            self._head_model_error_logged = True
                    finally:
                        with self._head_worker_lock:
                            self._head_worker_busy = False
        except Exception as exc:
            with self._head_worker_lock:
                self._head_worker_last_error = str(exc)[:180]
            if not self._head_model_error_logged:
                print(f'[WARN] Head CPU worker stopped: {exc}', flush=True)
                self._head_model_error_logged = True

    def _consume_head_worker_result(self, now):
        """Apply only the newest completed CPU head batch on the main thread."""
        with self._head_worker_lock:
            packet = self._head_worker_latest
            if not packet or int(packet.get('job_id', 0)) <= self._head_worker_consumed_job_id:
                return
            self._head_worker_consumed_job_id = int(packet['job_id'])
            detections = dict(packet.get('detections', {}))
            completed_at = float(packet.get('completed_at', now))
            self._head_inference_ms = float(packet.get('duration_ms', 0.0))

        for track_id, measurement in detections.items():
            raw_u = float(measurement.get('raw_u', 0.5))
            raw_v = float(measurement.get('raw_v', 0.145))
            confidence_value = float(measurement.get('confidence', 0.0))
            cache = self._head_point_cache.get(track_id)
            norm_x, norm_y = raw_u, raw_v
            head_w_rel = float(measurement.get('head_w_rel', 0.0))
            head_h_rel = float(measurement.get('head_h_rel', 0.0))
            head_aspect = float(measurement.get('head_aspect', 1.0))
            if cache and completed_at - float(cache.get('last_detection', 0.0)) <= 0.32:
                previous_u = float(cache.get('u', norm_x))
                previous_v = float(cache.get('v', norm_y))
                previous_conf = float(cache.get('confidence', 0.0))
                # Trust a strong, geometrically valid measurement quickly;
                # apply more damping only to weak detections to avoid jitter.
                if confidence_value >= 0.60:
                    alpha = 0.86
                elif confidence_value >= 0.38:
                    alpha = 0.74
                else:
                    alpha = 0.58
                # A sudden weak jump is more likely a neighboring head/false box.
                jump = math.hypot((norm_x - previous_u) * 1.0, (norm_y - previous_v) * 1.6)
                if jump > 0.34 and confidence_value < max(0.50, previous_conf + 0.08):
                    alpha = 0.28
                norm_x = previous_u + alpha * (norm_x - previous_u)
                norm_y = previous_v + alpha * (norm_y - previous_v)
            norm_x = max(0.01, min(0.99, norm_x))
            norm_y = max(-0.06, min(0.48, norm_y))
            self._head_point_cache[track_id] = {
                'u': norm_x,
                'v': norm_y,
                'raw_u': raw_u,
                'raw_v': raw_v,
                'head_w_rel': head_w_rel,
                'head_h_rel': head_h_rel,
                'head_aspect': head_aspect,
                'last_detection': completed_at,
                'confidence': confidence_value,
            }

    def _detect_head_points(self, frame, records):
        """Submit small head ROIs to a background CPU worker and return cached anchors."""
        if self.head_model is None or frame is None or not records:
            return {}

        now = time.perf_counter()
        self._consume_head_worker_result(now)
        fov_radius = self._fov_radius(frame.shape)
        fov_cx, fov_cy = self._fov_center_local()
        locked_track_id = getattr(self, '_aim_locked_track_id', None)
        refresh_due = (now - float(self._head_inference_last_at)) >= self._head_inference_interval

        if refresh_due and fov_radius > 0.0:
            frame_h, frame_w = frame.shape[:2]
            roi_items = []
            for record in records:
                if record.get('is_friend', False):
                    continue
                track_id = record.get('track_id')
                box = record.get('box')
                if track_id is None or not box or len(box) != 4:
                    continue
                x1, y1, x2, y2 = map(float, box)
                bw = max(2.0, x2 - x1)
                bh = max(4.0, y2 - y1)
                if (track_id != locked_track_id
                        and self._choose_fov_anchor(x1, y1, x2, y2, fov_radius) is None):
                    continue
                left = max(0, int(round(x1 - 0.18 * bw)))
                right = min(frame_w, int(round(x2 + 0.18 * bw)))
                top = max(0, int(round(y1 - 0.08 * bh)))
                bottom = min(frame_h, int(round(y1 + 0.48 * bh)))
                if right - left < 12 or bottom - top < 12:
                    continue
                roi = frame[top:bottom, left:right]
                if roi.size == 0:
                    continue
                nearest_x = min(max(fov_cx, x1), x2)
                nearest_y = min(max(fov_cy, y1), y2)
                distance = math.hypot(nearest_x - fov_cx, nearest_y - fov_cy)
                roi_items.append({
                    'track_id': int(track_id),
                    'roi': roi.copy(),
                    'left': left,
                    'top': top,
                    'box': (x1, y1, x2, y2),
                    'distance': distance,
                    'locked': track_id == locked_track_id,
                })

            roi_items.sort(key=lambda item: (not item['locked'], item['distance']))
            # Keep the active lock first, plus the nearest acquisition candidate.
            # A single latest-wins batch prevents stale work from accumulating.
            if roi_items:
                self._start_head_inference_worker()
                self._head_inference_last_at = now
                self._head_last_locked_track_id = locked_track_id
                job = {'items': roi_items[:2]}
                with self._head_worker_lock:
                    # Single-slot latest-wins queue: no stale backlog can build up.
                    self._head_worker_pending = job
                self._head_worker_wakeup.set()

        results = self._cached_head_points_for_records(records, now)
        # Keep the measured CPU worker latency visible in both Debug windows.
        with self._head_worker_lock:
            self._head_inference_ms = float(self._head_worker_last_ms)
        return results

    def _point_for_box(self, x1, y1, x2, y2):
        width = max(1.0, float(x2) - float(x1))
        height = max(1.0, float(y2) - float(y1))
        px = float(x1) + width * self.aim_point_u
        py = float(y1) + height * self.aim_point_v
        return px, py

    def _find_drag_box(self):
        """Find the current detection corresponding to the box being dragged."""
        if self._aim_point_drag_box_center is None or not self._debug_boxes_cache:
            return None
        old_cx, old_cy = self._aim_point_drag_box_center
        candidates = []
        for box in self._debug_boxes_cache:
            x1, y1, x2, y2 = box
            cx = (x1 + x2) / 2.0
            cy = (y1 + y2) / 2.0
            distance = math.hypot(cx - old_cx, cy - old_cy)
            candidates.append((distance, box, cx, cy))
        distance, box, cx, cy = min(candidates, key=lambda item: item[0])
        x1, y1, x2, y2 = box
        max_distance = max(30.0, 0.45 * max(x2 - x1, y2 - y1))
        if distance > max_distance:
            return None
        self._aim_point_drag_box_center = (cx, cy)
        return box

    def _set_aim_point_from_cursor(self, x, y, box):
        x1, y1, x2, y2 = box
        width = max(1.0, float(x2 - x1))
        height = max(1.0, float(y2 - y1))
        self.aim_point_u = max(0.0, min(1.0, (float(x) - x1) / width))
        self.aim_point_v = max(0.0, min(1.0, (float(y) - y1) / height))

    def _on_debug_mouse_event(self, event, x, y, flags, param):
        """Drag the green point in HEAD mode; its normalized position is autosaved."""
        if not (self.debug_mode or self.debug_record_mode):
            return
        if str(getattr(self, 'aim_target_mode', 'all')).lower() != 'head':
            return
        if event == cv2.EVENT_LBUTTONDOWN:
            hits = []
            for box in self._debug_boxes_cache:
                x1, y1, x2, y2 = box
                px, py = self._point_for_box(x1, y1, x2, y2)
                radius = max(12.0, min(22.0, (y2 - y1) * 0.06))
                distance = math.hypot(float(x) - px, float(y) - py)
                if distance <= radius:
                    hits.append((distance, box))
            if hits:
                _, box = min(hits, key=lambda item: item[0])
                x1, y1, x2, y2 = box
                self._aim_point_dragging = True
                self._aim_point_drag_box_center = (
                    (x1 + x2) / 2.0, (y1 + y2) / 2.0
                )
                self._set_aim_point_from_cursor(x, y, box)
        elif event == cv2.EVENT_MOUSEMOVE and self._aim_point_dragging:
            box = self._find_drag_box()
            if box is not None:
                self._set_aim_point_from_cursor(x, y, box)
        elif event == cv2.EVENT_LBUTTONUP and self._aim_point_dragging:
            self._aim_point_dragging = False
            self._aim_point_drag_box_center = None
            self._save_aim_point_calibration()

    def _debug_hotkey_watch_loop(self):
        """Listen for physical Numpad 1, 2, and 3, independent of NumLock state.

        Scan codes distinguish the keypad keys from navigation keys and their
        extended-key equivalents. F4 remains a fallback toggle for Debug 1.
        """
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [
                ('vkCode', wintypes.DWORD),
                ('scanCode', wintypes.DWORD),
                ('flags', wintypes.DWORD),
                ('time', wintypes.DWORD),
                ('dwExtraInfo', ctypes.c_size_t),
            ]

        WH_KEYBOARD_LL = 13
        HC_ACTION = 0
        WM_KEYDOWN = 0x0100
        WM_KEYUP = 0x0101
        WM_SYSKEYDOWN = 0x0104
        WM_SYSKEYUP = 0x0105
        WM_QUIT = 0x0012
        LLKHF_EXTENDED = 0x01

        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = LRESULT
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostThreadMessageW.restype = wintypes.BOOL
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        self._debug_hotkey_thread_id = kernel32.GetCurrentThreadId()
        state = {'numpad1': False, 'f4': False, 'numpad2': False, 'numpad3': False}
        hook_handle = ctypes.c_void_p()

        def hook_callback(n_code, w_param, l_param):
            if n_code == HC_ACTION:
                try:
                    key = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    is_keypad_one = (key.scanCode == 0x4F and not (key.flags & LLKHF_EXTENDED))
                    is_keypad_two = (key.scanCode == 0x50 and not (key.flags & LLKHF_EXTENDED))
                    is_keypad_three = (key.scanCode == 0x51 and not (key.flags & LLKHF_EXTENDED))
                    is_f4 = (key.vkCode == 0x73)
                    is_down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
                    is_up = w_param in (WM_KEYUP, WM_SYSKEYUP)

                    if is_keypad_one or is_f4:
                        state_key = 'numpad1' if is_keypad_one else 'f4'
                        if is_down and not state[state_key]:
                            self._debug_toggle_events.put(True)
                        if is_down:
                            state[state_key] = True
                        elif is_up:
                            state[state_key] = False

                    if is_keypad_two:
                        if is_down and not state['numpad2']:
                            self._debug_record_toggle_events.put(True)
                        if is_down:
                            state['numpad2'] = True
                        elif is_up:
                            state['numpad2'] = False

                    if is_keypad_three:
                        if is_down and not state['numpad3']:
                            self._self_id_toggle_events.put(True)
                        if is_down:
                            state['numpad3'] = True
                        elif is_up:
                            state['numpad3'] = False
                except Exception:
                    pass
            return user32.CallNextHookEx(hook_handle, n_code, w_param, l_param)

        self._debug_hook_proc = HOOKPROC(hook_callback)
        hook_handle = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._debug_hook_proc, kernel32.GetModuleHandleW(None), 0
        )
        if not hook_handle:
            self._debug_hotkey_error = ctypes.get_last_error()
            self._debug_hook_available = False
            self._debug_hotkey_ready.set()
            return

        self._debug_keyboard_hook = hook_handle
        self._debug_hook_available = True
        self._debug_hotkey_ready.set()
        msg = wintypes.MSG()
        try:
            while not self._debug_hotkey_stop.is_set():
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result <= 0 or msg.message == WM_QUIT:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnhookWindowsHookEx(hook_handle)
            self._debug_keyboard_hook = None
            self._debug_hook_available = False

    def _start_debug_hotkey_watcher(self):
        if self._debug_hotkey_thread and self._debug_hotkey_thread.is_alive():
            return
        self._debug_hotkey_stop.clear()
        self._debug_hotkey_ready.clear()
        self._debug_hotkey_error = None
        self._debug_hotkey_thread = threading.Thread(
            target=self._debug_hotkey_watch_loop,
            name='LunarDebugHotkey',
            daemon=True,
        )
        self._debug_hotkey_thread.start()
        self._debug_hotkey_ready.wait(timeout=1.0)
        if self._debug_hook_available:
            print('[INFO] Debug hotkeys registered: Numpad 1 (normal), Numpad 2 (record), Numpad 3 (ignore region), F4 (fallback).', flush=True)
        else:
            print(
                f'[WARN] Numpad hook unavailable ({self._debug_hotkey_error}); '
                'fallback: use F4 / Numpad 1, 2, and 3 with NumLock ON.',
                flush=True,
            )

    def _stop_debug_hotkey_watcher(self):
        self._debug_hotkey_stop.set()
        if self._debug_hotkey_thread and self._debug_hotkey_thread.is_alive():
            if self._debug_hotkey_thread_id:
                try:
                    user32 = ctypes.WinDLL('user32', use_last_error=True)
                    user32.PostThreadMessageW(
                        self._debug_hotkey_thread_id, 0x0012, 0, 0
                    )
                except Exception:
                    pass
            self._debug_hotkey_thread.join(timeout=1.0)
        self._debug_hotkey_thread = None
        self._debug_hotkey_thread_id = None

    @staticmethod
    def _get_videos_directory():
        """Resolve the actual Windows Videos known folder, including redirection."""
        try:
            class GUID(ctypes.Structure):
                _fields_ = [
                    ('Data1', ctypes.c_uint32),
                    ('Data2', ctypes.c_uint16),
                    ('Data3', ctypes.c_uint16),
                    ('Data4', ctypes.c_ubyte * 8),
                ]

            folder_guid = uuid.UUID('18989B1D-99B5-455B-841C-AB7C74E4DDFC')
            known_folder = GUID.from_buffer_copy(folder_guid.bytes_le)
            shell32 = ctypes.WinDLL('shell32', use_last_error=True)
            ole32 = ctypes.WinDLL('ole32', use_last_error=True)
            path_ptr = ctypes.c_wchar_p()
            shell32.SHGetKnownFolderPath.argtypes = [
                ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                ctypes.POINTER(ctypes.c_wchar_p),
            ]
            shell32.SHGetKnownFolderPath.restype = ctypes.c_long
            result = shell32.SHGetKnownFolderPath(
                ctypes.byref(known_folder), 0, None, ctypes.byref(path_ptr)
            )
            if result == 0 and path_ptr.value:
                videos_dir = path_ptr.value
                ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
                ole32.CoTaskMemFree(ctypes.cast(path_ptr, ctypes.c_void_p))
                return videos_dir
        except Exception:
            pass
        return os.path.join(os.path.expanduser('~'), 'Videos')

    def _start_debug_recording_view(self):
        """Open the second Debug window and prepare an MP4 recording."""
        if self.debug_record_mode:
            return
        writer = None
        output_path = None
        frame_size = (
            max(1, int(self.capture_width) + self.DEBUG_PANEL_WIDTH),
            max(1, int(self.capture_height)),
        )
        videos_dir = self._get_videos_directory()
        try:
            self._debug_last_render_at = 0.0
            self._debug_last_display_at = None
            self._debug_preview_fps = 0.0
            os.makedirs(videos_dir, exist_ok=True)
            stamp = time.strftime('%Y%m%d_%H%M%S')
            base_name = f'Lunar_Debug_{stamp}'

            cv2.namedWindow(self.debug_record_window_name, cv2.WINDOW_AUTOSIZE)
            cv2.setMouseCallback(self.debug_record_window_name, self._on_debug_mouse_event)
            cv2.moveWindow(self.debug_record_window_name, 70, 70)
            startup_preview = np.zeros((180, 560, 3), dtype=np.uint8)
            cv2.putText(
                startup_preview, 'DEBUG RECORDING STARTING...', (18, 85),
                cv2.FONT_HERSHEY_SIMPLEX, 0.72, (0, 0, 255), 2, cv2.LINE_AA,
            )
            cv2.imshow(self.debug_record_window_name, startup_preview)
            cv2.waitKey(1)

            # MP4 is preferred. Fall back to AVI/MJPG on systems without MP4V.
            for extension, codec in (('.mp4', 'mp4v'), ('.avi', 'MJPG')):
                candidate = os.path.join(videos_dir, base_name + extension)
                suffix = 2
                while os.path.exists(candidate):
                    candidate = os.path.join(videos_dir, f'{base_name}_{suffix}{extension}')
                    suffix += 1
                candidate_writer = cv2.VideoWriter(
                    candidate, cv2.VideoWriter_fourcc(*codec),
                    self._debug_recording_fps, frame_size,
                )
                if candidate_writer.isOpened():
                    writer = candidate_writer
                    output_path = candidate
                    break
                candidate_writer.release()

            if writer is None:
                raise RuntimeError('OpenCV não conseguiu iniciar a gravação MP4 nem AVI.')

            self._debug_recording_writer = writer
            self._debug_recording_path = output_path
            self._debug_recording_size = frame_size
            self._debug_recording_next_frame_at = None
            self.debug_record_window_created = True
            self.debug_record_mode = True
            print(
                f'[INFO] DEBUG RECORDING ENABLED — Numpad 2 para parar e salvar: {output_path}',
                flush=True,
            )
        except Exception as exc:
            if writer is not None:
                writer.release()
            self._debug_recording_writer = None
            self._debug_recording_path = None
            self._debug_recording_size = None
            self._debug_recording_next_frame_at = None
            self.debug_record_mode = False
            self.debug_record_window_created = False
            try:
                cv2.destroyWindow(self.debug_record_window_name)
                cv2.waitKey(1)
            except cv2.error:
                pass
            print(f'[ERROR] Não foi possível iniciar a gravação do Debug: {exc}', flush=True)

    def _write_debug_recording_frame(self, debug_frame):
        """Write one fresh Debug image per preview update at the configured FPS."""
        writer = self._debug_recording_writer
        if writer is None or not self.debug_record_mode:
            return
        try:
            expected_width, expected_height = self._debug_recording_size
            if (debug_frame.shape[1], debug_frame.shape[0]) != (expected_width, expected_height):
                output_frame = cv2.resize(
                    debug_frame, (expected_width, expected_height),
                    interpolation=cv2.INTER_AREA,
                )
            else:
                output_frame = debug_frame

            now = time.perf_counter()
            interval = 1.0 / self._debug_recording_fps
            if self._debug_recording_next_frame_at is None:
                self._debug_recording_next_frame_at = now
            frames_due = int((now - self._debug_recording_next_frame_at) / interval) + 1
            if frames_due <= 0:
                return
            # Avoid long blocking writes after a temporary inference stall.
            if frames_due > 6:
                frames_due = 1
                self._debug_recording_next_frame_at = now
            for _ in range(frames_due):
                writer.write(output_frame)
            self._debug_recording_next_frame_at += frames_due * interval
        except Exception as exc:
            print(f'[ERROR] Falha ao gravar frame do Debug: {exc}', flush=True)
            self._stop_debug_recording_view()

    def _stop_debug_recording_view(self):
        """Stop recording, finalize the file, and close the Numpad-2 window."""
        self.debug_record_mode = False
        writer = self._debug_recording_writer
        output_path = self._debug_recording_path
        self._debug_recording_writer = None
        self._debug_recording_path = None
        self._debug_recording_size = None
        self._debug_recording_next_frame_at = None
        if writer is not None:
            try:
                writer.release()
            except Exception as exc:
                print(f'[WARN] Erro ao finalizar o vídeo: {exc}', flush=True)
        if self.debug_record_window_created:
            try:
                cv2.destroyWindow(self.debug_record_window_name)
                cv2.waitKey(1)
            except cv2.error:
                pass
        self.debug_record_window_created = False
        if output_path and os.path.isfile(output_path):
            try:
                size_bytes = os.path.getsize(output_path)
            except OSError:
                size_bytes = 0
            if size_bytes > 0:
                print(f'[INFO] DEBUG RECORDING SAVED: {output_path} ({size_bytes:,} bytes)', flush=True)
            else:
                print(f'[WARN] O arquivo de gravação ficou vazio: {output_path}', flush=True)

    def update_debug_toggle(self):
        pending_debug = 0
        pending_record = 0
        pending_self_id = 0
        while True:
            try:
                self._debug_toggle_events.get_nowait()
                pending_debug += 1
            except queue.Empty:
                break
        while True:
            try:
                self._debug_record_toggle_events.get_nowait()
                pending_record += 1
            except queue.Empty:
                break
        while True:
            try:
                self._self_id_toggle_events.get_nowait()
                pending_self_id += 1
            except queue.Empty:
                break

        if not self._debug_hook_available:
            # Low-level hook normally handles NumLock OFF as well. This polling
            # fallback supports keypad digits with NumLock ON plus F4 for Debug 1.
            numpad1_down = bool(win32api.GetAsyncKeyState(0x61) & 0x8000)
            f4_down = bool(win32api.GetAsyncKeyState(0x73) & 0x8000)
            numpad2_down = bool(win32api.GetAsyncKeyState(0x62) & 0x8000)
            numpad3_down = bool(win32api.GetAsyncKeyState(0x63) & 0x8000)
            debug_down = numpad1_down or f4_down
            if debug_down and not self.numpad1_was_down:
                pending_debug += 1
            if numpad2_down and not self.numpad2_was_down:
                pending_record += 1
            if numpad3_down and not self.numpad3_was_down:
                pending_self_id += 1
            self.numpad1_was_down = debug_down
            self.numpad2_was_down = numpad2_down
            self.numpad3_was_down = numpad3_down

        if pending_self_id % 2:
            if self.self_id_mode:
                self._stop_self_id_window()
            else:
                self._start_self_id_window()

        if pending_debug % 2:
            if not self.debug_mode:
                try:
                    self._debug_last_render_at = 0.0
                    self._debug_last_display_at = None
                    self._debug_preview_fps = 0.0
                    cv2.namedWindow(self.debug_window_name, cv2.WINDOW_AUTOSIZE)
                    cv2.setMouseCallback(self.debug_window_name, self._on_debug_mouse_event)
                    cv2.moveWindow(self.debug_window_name, 30, 30)
                    startup_preview = np.zeros((180, 520, 3), dtype=np.uint8)
                    cv2.putText(
                        startup_preview, 'DEBUG VIEW STARTING...', (18, 85),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2, cv2.LINE_AA,
                    )
                    cv2.imshow(self.debug_window_name, startup_preview)
                    cv2.waitKey(1)
                    self.debug_window_created = True
                    self.debug_mode = True
                    print('[INFO] DEBUG VIEW ENABLED — Numpad 1 to hide.', flush=True)
                except cv2.error as exc:
                    self.debug_mode = False
                    self.debug_window_created = False
                    print(f'[ERROR] Could not create debug window: {exc}', flush=True)
            else:
                self.debug_mode = False
                self.debug_window_created = False
                if not self.debug_record_mode:
                    self._aim_point_dragging = False
                    self._aim_point_drag_box_center = None
                try:
                    cv2.destroyWindow(self.debug_window_name)
                    cv2.waitKey(1)
                except cv2.error:
                    pass
                print('[INFO] DEBUG VIEW DISABLED', flush=True)

        if pending_record % 2:
            if not self.debug_record_mode:
                self._start_debug_recording_view()
            else:
                self._stop_debug_recording_view()

        self._pump_self_id_window()

    def _sample_resource_usage(self):
        """Sample process RAM and CUDA allocator figures at most once per second."""
        now = time.perf_counter()
        if now - float(getattr(self, '_resource_sample_at', 0.0)) < 1.0:
            return
        self._resource_sample_at = now
        try:
            import psutil
            self._process_ram_mb = psutil.Process(os.getpid()).memory_info().rss / (1024.0 * 1024.0)
        except Exception:
            # Native Windows fallback; psutil remains optional.
            try:
                class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
                    _fields_ = [
                        ('cb', wintypes.DWORD),
                        ('PageFaultCount', wintypes.DWORD),
                        ('PeakWorkingSetSize', ctypes.c_size_t),
                        ('WorkingSetSize', ctypes.c_size_t),
                        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                        ('PagefileUsage', ctypes.c_size_t),
                        ('PeakPagefileUsage', ctypes.c_size_t),
                        ('PrivateUsage', ctypes.c_size_t),
                    ]
                psapi = ctypes.WinDLL('psapi', use_last_error=True)
                psapi.GetProcessMemoryInfo.argtypes = [
                    wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), wintypes.DWORD
                ]
                psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
                kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel32.GetCurrentProcess.restype = wintypes.HANDLE
                counters = PROCESS_MEMORY_COUNTERS_EX()
                counters.cb = ctypes.sizeof(counters)
                if psapi.GetProcessMemoryInfo(
                    kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
                ):
                    self._process_ram_mb = counters.WorkingSetSize / (1024.0 * 1024.0)
                else:
                    self._process_ram_mb = -1.0
            except Exception:
                self._process_ram_mb = -1.0
        try:
            if torch.cuda.is_available():
                self._cuda_allocated_mb = torch.cuda.memory_allocated() / (1024.0 * 1024.0)
                self._cuda_reserved_mb = torch.cuda.memory_reserved() / (1024.0 * 1024.0)
            else:
                self._cuda_allocated_mb = 0.0
                self._cuda_reserved_mb = 0.0
        except Exception:
            pass

    def show_debug_frame(self, frame, boxes_cpu, own_player_flags, trigger_box_found, best_aim_target=None, friend_flags=None, friend_reasons=None):
        """Show YOLO boxes, friend exclusions, Trigger Bot regions, and Aim FOV."""
        if not (self.debug_mode or self.debug_record_mode):
            return

        now = time.perf_counter()
        render_interval = 1.0 / max(1.0, float(self._debug_preview_fps_limit))
        if now - self._debug_last_render_at < render_interval:
            return
        if self._debug_last_display_at is not None:
            display_dt = max(0.001, now - self._debug_last_display_at)
            display_sample = 1.0 / display_dt
            self._debug_preview_fps = (
                display_sample if self._debug_preview_fps <= 0.0
                else 0.78 * self._debug_preview_fps + 0.22 * display_sample
            )
        self._debug_last_render_at = now
        self._debug_last_display_at = now

        try:
            debug_frame = frame.copy()
            current_valid_boxes = []
            for index, coords in enumerate(boxes_cpu):
                x1, y1, x2, y2 = (int(value) for value in coords)
                is_filtered = own_player_flags[index] if index < len(own_player_flags) else False
                is_friend = friend_flags[index] if friend_flags and index < len(friend_flags) else False
                reason = friend_reasons[index] if friend_reasons and index < len(friend_reasons) else ''
                meta = self._debug_detection_meta[index] if index < len(self._debug_detection_meta) else None
                is_self = bool((meta or {}).get('is_self', False))
                color = (40, 70, 255) if is_self else ((210, 170, 35) if is_friend else ((0, 145, 255) if is_filtered else (70, 210, 130)))

                # Corner brackets show the Universal box used as the complete
                # hitbox approximation for Aim Assist.
                width = max(1, x2 - x1)
                height = max(1, y2 - y1)
                arm = max(7, min(18, width // 4, height // 4))
                corners = (
                    ((x1, y1), (x1 + arm, y1), (x1, y1 + arm)),
                    ((x2, y1), (x2 - arm, y1), (x2, y1 + arm)),
                    ((x1, y2), (x1 + arm, y2), (x1, y2 - arm)),
                    ((x2, y2), (x2 - arm, y2), (x2, y2 - arm)),
                )
                head_mode = str(getattr(self, 'aim_target_mode', 'all')).lower() == 'head'
                if not head_mode or is_filtered or is_friend or is_self:
                    for origin, h_end, v_end in corners:
                        cv2.line(debug_frame, origin, h_end, color, 2, cv2.LINE_AA)
                if not head_mode and not is_filtered and not is_friend:
                    hb_left, hb_top, hb_right, hb_bottom = self._scaled_hitbox_bounds(x1, y1, x2, y2)
                    cv2.rectangle(
                        debug_frame,
                        (int(round(hb_left)), int(round(hb_top))),
                        (int(round(hb_right)), int(round(hb_bottom))),
                        (40, 190, 255), 1, cv2.LINE_AA,
                    )
                if is_self:
                    self_reason = str((meta or {}).get('filtered_reason') or 'SELF PLAYER')
                    label = f'{self_reason} {float((meta or {}).get("self_score", 0.0)):.2f}'
                elif is_friend:
                    label = f'FRIEND {reason}'.strip()
                elif is_filtered:
                    why = (meta or {}).get('filtered_reason', 'FILTER')
                    conf_value = (meta or {}).get('confidence', 0.0)
                    label = f'FILTERED {why} {conf_value:.2f}'
                else:
                    conf_value = (meta or {}).get('confidence', 0.0)
                    track_id = (meta or {}).get('track_id', '-')
                    confirmed = bool((meta or {}).get('confirmed', False))
                    label = f'ID {track_id} {conf_value:.2f} ' + ('READY' if confirmed else 'VERIFY')
                cv2.putText(
                    debug_frame, label, (x1, max(18, y1 - 7)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.34, color, 1, cv2.LINE_AA,
                )

                if not is_filtered and not is_friend:
                    current_valid_boxes.append((x1, y1, x2, y2))
                    if head_mode:
                        point_x, point_y = self._point_for_box(x1, y1, x2, y2)
                        point = (int(round(point_x)), int(round(point_y)))
                        # In HEAD mode, show a single-style green draggable point;
                        # no custom hitbox overlay is drawn.
                        cv2.circle(debug_frame, point, 10, (0, 0, 0), 2, cv2.LINE_AA)
                        cv2.circle(debug_frame, point, 6, (70, 245, 115), -1, cv2.LINE_AA)
                        cv2.circle(debug_frame, point, 2, (255, 255, 255), -1, cv2.LINE_AA)
                        cv2.putText(
                            debug_frame, 'HEAD POINT - DRAG',
                            (point[0] + 9, max(14, point[1] - 9)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.30, (70, 245, 115), 1, cv2.LINE_AA,
                        )
                    else:
                        point_x, point_y, point_mode = self._hitbox_anchor(
                            x1, y1, x2, y2, self.crosshair_x, self.crosshair_y
                        )
                        point = (int(round(point_x)), int(round(point_y)))
                        cv2.circle(debug_frame, point, 8, (0, 0, 0), 2, cv2.LINE_AA)
                        cv2.circle(debug_frame, point, 5, (70, 235, 110), -1, cv2.LINE_AA)
                        cv2.circle(debug_frame, point, 2, (255, 255, 255), -1, cv2.LINE_AA)
                        cv2.putText(
                            debug_frame,
                            f"HITBOX {point_mode} L{self.aim_hitbox_left_offset:+d} "
                            f"R{self.aim_hitbox_right_offset:+d} T{self.aim_hitbox_top_offset:+d} "
                            f"B{self.aim_hitbox_bottom_offset:+d}",
                            (point[0] + 7, max(14, point[1] - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.30, (70, 235, 110), 1, cv2.LINE_AA,
                        )

                if not is_filtered and not is_friend and self.trigger_enabled:
                    center_x = (float(x1) + float(x2)) / 2.0
                    center_y = (float(y1) + float(y2)) / 2.0
                    box_width = max(1.0, float(x2 - x1))
                    box_height = max(1.0, float(y2 - y1))
                    if any((self.trigger_box_left, self.trigger_box_right,
                            self.trigger_box_top, self.trigger_box_bottom)):
                        trigger_bounds = (
                            int(round(center_x - box_width * self.trigger_box_left / 100.0)),
                            int(round(center_y - box_height * self.trigger_box_top / 100.0)),
                            int(round(center_x + box_width * self.trigger_box_right / 100.0)),
                            int(round(center_y + box_height * self.trigger_box_bottom / 100.0)),
                        )
                        cv2.rectangle(
                            debug_frame, trigger_bounds[:2], trigger_bounds[2:],
                            (210, 80, 225), 1, cv2.LINE_AA,
                        )
                        cv2.putText(
                            debug_frame, 'TRIGGER HITBOX',
                            (trigger_bounds[0], max(14, trigger_bounds[1] - 4)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.34, (210, 80, 225), 1, cv2.LINE_AA,
                        )

            # Publish the latest valid boxes for mouse-drag calibration.
            # The Debug window is WINDOW_AUTOSIZE, so callback x/y equal frame pixels.
            self._debug_boxes_cache = current_valid_boxes

            # Draw the same configurable FOV on the Debug copy of the frame
            # sent to YOLO. Its center is the calibrated reticle plus X/Y offset.
            frame_h, frame_w = debug_frame.shape[:2]
            fov_cx, fov_cy = self._fov_center_local()
            center_x = int(round(min(max(0.0, fov_cx), frame_w - 1.0)))
            center_y = int(round(min(max(0.0, fov_cy), frame_h - 1.0)))
            fov_diameter = max(0, min(700, int(round(self.aim_fov_size)))) if self.aim_fov_visible else 0
            if fov_diameter > 0:
                fov_radius = max(1, int(round(fov_diameter / 2.0)))
                color_rgb = tuple(int(self.aim_fov_color[i:i + 2], 16) for i in (1, 3, 5))
                color_bgr = (color_rgb[2], color_rgb[1], color_rgb[0])
                cv2.circle(
                    debug_frame, (center_x, center_y), fov_radius,
                    (0, 0, 0), min(14, self.aim_fov_thickness + 4), cv2.LINE_AA,
                )
                cv2.circle(
                    debug_frame, (center_x, center_y), fov_radius,
                    color_bgr, self.aim_fov_thickness, cv2.LINE_AA,
                )
                fov_label = f'FOV: {fov_diameter}px | CENTER {self.aim_fov_offset_x:+d},{self.aim_fov_offset_y:+d}'
            elif not self.aim_fov_visible and self.aim_fov_size > 0:
                fov_label = 'FOV RING HIDDEN | ACQUISITION ACTIVE'
            else:
                fov_label = 'FOV SIZE 0 (NO ACQUISITION)'

            # Show the calibrated reticle separately from the FOV center.
            reticle_x = int(round(min(max(0.0, self.crosshair_x), frame_w - 1.0)))
            reticle_y = int(round(min(max(0.0, self.crosshair_y), frame_h - 1.0)))
            cv2.drawMarker(
                debug_frame, (reticle_x, reticle_y), (255, 255, 255),
                cv2.MARKER_CROSS, 20, 2, cv2.LINE_AA,
            )
            cv2.circle(debug_frame, (reticle_x, reticle_y), 3, (0, 255, 255), -1, cv2.LINE_AA)

            # Blue/orange point above is the raw detector measurement. This
            # green cross is the point actually consumed by the motion controller.
            # Keeping both visible makes filter lag and camera-motion compensation
            # easy to inspect without reading pixels back from the Debug window.
            if (best_aim_target is not None
                    and best_aim_target.get('tracking_state') == 'LOCKED'):
                # Green is reserved for a fresh detector-backed measurement.
                control_point = (
                    int(round(min(max(0.0, self._filtered_aim_x), frame_w - 1.0))),
                    int(round(min(max(0.0, self._filtered_aim_y), frame_h - 1.0))),
                )
                cv2.line(debug_frame, (reticle_x, reticle_y), control_point,
                         (70, 230, 120), 1, cv2.LINE_AA)
                cv2.drawMarker(debug_frame, control_point, (70, 230, 120),
                               cv2.MARKER_TILTED_CROSS, 13, 2, cv2.LINE_AA)
                cv2.putText(debug_frame, 'CTRL VALID',
                            (control_point[0] + 7, min(frame_h - 8, control_point[1] + 16)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.31, (70, 230, 120), 1, cv2.LINE_AA)

            panel_width = self.DEBUG_PANEL_WIDTH
            panel = np.full((frame_h, panel_width, 3), (18, 20, 27), dtype=np.uint8)
            panel_color = (225, 230, 238)
            muted_color = (150, 160, 175)
            accent_color = (80, 205, 255)
            good_color = (85, 220, 145)
            warn_color = (60, 170, 255)

            def panel_text(y, value, color=panel_color, scale=0.39, thickness=1, x=12, limit=42):
                cv2.putText(
                    panel, str(value)[:limit], (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                    scale, color, thickness, cv2.LINE_AA,
                )

            cv2.putText(
                panel, 'LUNAR DETECTOR', (12, 22), cv2.FONT_HERSHEY_SIMPLEX,
                0.48, (255, 255, 255), 1, cv2.LINE_AA,
            )
            cv2.line(panel, (12, 31), (panel_width - 12, 31), (65, 75, 90), 1, cv2.LINE_AA)

            active_features = []
            if self.aim_enabled:
                active_features.append('AIM')
            if self.trigger_enabled:
                active_features.append('TRIGGER')
            feature_label = '+'.join(active_features) if active_features else 'NONE'
            panel_text(51, f'{feature_label}: ' + ('ACTIVE' if self.master_enabled else 'ARMED OFF'),
                       good_color if self.master_enabled else warn_color, 0.37)
            panel_text(73, f'PASS: {self._last_inference_scale}', accent_color, 0.31)
            flow_state = 'ON' if self._camera_motion_affine is not None else 'FALLBACK'
            panel_text(94, f'FPS RUN:{self._runtime_fps:.1f} DBG:{self._debug_preview_fps:.1f}/{self._debug_preview_fps_limit:.0f}', muted_color, 0.30)
            panel_text(112, f'CAP:{self._last_capture_ms:.1f} INF:{self._last_base_inference_ms:.1f}/{self._last_inference_ms:.1f} AGE:{self._last_frame_age_ms:.1f}ms', muted_color, 0.25)
            panel_text(128, f'POST:{self._last_postprocess_ms:.1f} FLOW:{self._last_camera_flow_ms:.1f} IMG:{self._last_detector_imgsz}px', muted_color, 0.25)
            panel_text(146, f'FLOW:{flow_state}/{self._camera_motion_inliers} GRID:{getattr(self, "_camera_motion_cells", 0)}/{4 if getattr(self, "_camera_motion_model", "") == "CORNER PHASE" else 9} Q:{self._camera_motion_quality:.2f}', accent_color, 0.27)
            flow_detail = getattr(self, '_camera_motion_reject_reason', 'N/A')
            panel_text(162, f'FLOW CHECK:{flow_detail} COV:{self._camera_motion_coverage:.2f} RES:{self._camera_motion_residual:.1f}', muted_color, 0.24)
            panel_text(178, f'CAM LAT:{self._adaptive_latency_s * 1000.0:.0f}ms GAIN:{"CAL" if self._adaptive_gain_ready_x and self._adaptive_gain_ready_y else "EST"}', accent_color, 0.25)
            panel_text(194, f'ZOOM REASON: {self._last_zoom_reason}', accent_color, 0.25)

            raw_count = len(boxes_cpu)
            low_conf_count = sum(1 for item in self._debug_detection_meta if item and item.get('filtered_reason') == 'LOW CONF')
            shape_count = sum(1 for item in self._debug_detection_meta if item and item.get('filtered_reason') == 'SHAPE')
            class_count = sum(1 for item in self._debug_detection_meta if item and item.get('filtered_reason') == 'CLASS')
            ignored_region_count = sum(1 for item in self._debug_detection_meta if item and item.get('is_ignore_region'))
            accepted_count = sum(
                1 for index in range(raw_count)
                if index < len(own_player_flags) and not own_player_flags[index]
            )
            friend_count = sum(1 for value in (friend_flags or []) if value)
            in_fov_count = 0
            fov_radius = self._fov_radius(debug_frame.shape)
            fov_count_x, fov_count_y = self._fov_center_local()
            for index, coords in enumerate(boxes_cpu):
                if index >= len(own_player_flags) or own_player_flags[index]:
                    continue
                if friend_flags and index < len(friend_flags) and friend_flags[index]:
                    continue
                x1, y1, x2, y2 = map(float, coords)
                near_x = min(max(float(fov_count_x), x1), x2)
                near_y = min(max(float(fov_count_y), y1), y2)
                if math.hypot(near_x - fov_count_x, near_y - fov_count_y) <= fov_radius:
                    in_fov_count += 1

            trigger_state = 'IN' if trigger_box_found else 'OUT'
            panel_text(214, f'RAW:{raw_count} VALID:{accepted_count}', panel_color, 0.37)
            panel_text(234, f'FOV:{in_fov_count} FR:{friend_count} TRIG:{trigger_state}', good_color if in_fov_count else warn_color, 0.34)
            panel_text(254, f'MIN CONF:{self._last_effective_confidence * 100:.0f}% MENU:{self.conf * 100:.0f}%', accent_color, 0.30)
            region_state = 'ON' if self._ignore_region_norm is not None else 'OFF'
            panel_text(274, f'IGNORE:{region_state} ({ignored_region_count}) LOW:{low_conf_count} SHAPE:{shape_count} CLASS:{class_count}', muted_color, 0.29)

            if best_aim_target is not None:
                track_state = best_aim_target.get('tracking_state', 'LOCKED')
                track_id = best_aim_target.get('track_id', getattr(self, '_aim_locked_track_id', None))
            elif getattr(self, '_aim_locked_box', None) is not None:
                track_state = 'WAIT DETECTION'
                track_id = getattr(self, '_aim_locked_track_id', None)
            else:
                track_state = 'IDLE'
                track_id = None
            track_id_text = f'#{track_id}' if track_id is not None else 'NONE'
            lost_for = float(best_aim_target.get('lost_for', 0.0)) if best_aim_target else 0.0
            panel_text(294, f'TRACK:{track_state} {track_id_text} MISS:{self._aim_lock_misses}',
                       good_color if track_state == 'LOCKED' else warn_color, 0.30)
            cv2.line(panel, (12, 304), (panel_width - 12, 304), (65, 75, 90), 1, cv2.LINE_AA)
            panel_text(324, f'LOCK LOST: {lost_for * 1000.0:.0f} ms', warn_color if lost_for > 0 else muted_color, 0.30)

            anchor_mode = (
                best_aim_target.get('anchor_mode', self._aim_anchor_mode)
                if best_aim_target else self._aim_anchor_mode
            ) or '--'
            anchor_mode_label = (
                'HEAD / DRAGGABLE POINT' if self.aim_target_mode == 'head' else
                ('RELATIVE / INSIDE' if anchor_mode == 'INSIDE' else
                 'RELATIVE / EDGE' if anchor_mode == 'EDGE' else 'IDLE')
            )
            if self._aim_point_u is not None and self._aim_point_v is not None:
                anchor_mode_label += f' U:{self._aim_point_u:.2f} V:{self._aim_point_v:.2f}'
            panel_text(344, f'ANCHOR: {anchor_mode_label}',
                       good_color if anchor_mode == 'INSIDE' else muted_color, 0.27)
            if best_aim_target is not None:
                panel_text(364, f'{"HEAD" if self.aim_target_mode == "head" else "HITBOX"} POINT: {best_aim_target["x"]:.1f},{best_aim_target["y"]:.1f}', good_color, 0.29)
            else:
                panel_text(364, f'{"HEAD" if self.aim_target_mode == "head" else "HITBOX"} POINT: --', muted_color, 0.29)
            panel_text(392, 'CANDIDATES', (255, 255, 255), 0.35)

            candidate_indices = [
                index for index, meta in enumerate(self._debug_detection_meta)
                if meta is not None
            ]
            candidate_indices.sort(
                key=lambda index: float(self._debug_detection_meta[index].get('confidence', 0.0)),
                reverse=True,
            )
            line_y = 414
            shown = 0
            for index in candidate_indices[:2]:
                meta = self._debug_detection_meta[index] or {}
                confidence_value = float(meta.get('confidence', 0.0))
                coords = boxes_cpu[index]
                box_width = max(1, int(round(float(coords[2]) - float(coords[0]))))
                box_height = max(1, int(round(float(coords[3]) - float(coords[1]))))
                reason = meta.get('filtered_reason', '')
                if index < len(friend_flags) and friend_flags[index]:
                    status = 'FRIEND'
                    color = (205, 175, 65)
                elif reason:
                    status = reason
                    color = warn_color
                else:
                    track_value = meta.get('track_id', '-')
                    status = f'ID {track_value} READY' if meta.get('confirmed') else f'ID {track_value} VERIFY'
                    color = good_color
                panel_text(line_y, f'{status} {confidence_value:.2f}', color, 0.31)
                panel_text(line_y + 16, f'BOX {box_width}x{box_height} px', muted_color, 0.28)
                line_y += 32
                shown += 1
            if shown == 0:
                panel_text(line_y, 'No boxes returned by YOLO', muted_color, 0.30)

            # Same live decision/output diagnostics are rendered into both
            # windows because NUM1 and NUM2 share this exact debug_output frame.
            diag = self._get_aim_diagnostics()
            diag_x = 270
            cv2.line(panel, (255, 38), (255, frame_h - 18), (65, 75, 90), 1, cv2.LINE_AA)
            panel_text(22, 'AIM / MOUSE PIPELINE', (255, 255, 255), 0.43, x=diag_x, limit=30)
            decision = str(diag.get('decision', 'BLOCKED'))
            reason = str(diag.get('reason', 'UNKNOWN'))
            decision_color = good_color if decision == 'ALLOW' else (warn_color if decision == 'HOLD' else (90, 160, 255))
            panel_text(51, f'DECISION: {decision}', decision_color, 0.34, x=diag_x, limit=28)
            panel_text(72, f'REASON: {reason}', warn_color if decision == 'BLOCKED' else muted_color, 0.30, x=diag_x, limit=30)
            panel_text(94, f'ERROR X:{diag.get("error_x", 0.0):+.1f} Y:{diag.get("error_y", 0.0):+.1f}', panel_color, 0.30, x=diag_x, limit=30)
            panel_text(114, f'DIST:{diag.get("distance", 0.0):.1f}px APPROACH:{diag.get("approach", 0.0) * 100:.0f}%', accent_color, 0.28, x=diag_x, limit=31)
            panel_text(134, f'SPEED:{self.aim_speed:.2f} FORCE:{self.aim_strength:.2f}', accent_color, 0.31, x=diag_x, limit=34)
            raw_state = 'ON' if self._raw_mouse_input_available else 'OFF'
            gain_state = 'CAL' if diag.get('gain_calibrated', False) else 'EST'
            panel_text(154, f'RATE:{diag.get("response_rate", 0.0):.1f}/s CAP:{diag.get("speed_cap_counts", 0)} G:{gain_state}', muted_color, 0.26, x=diag_x, limit=40)
            panel_text(184, 'MOUSE OUTPUT', (255, 255, 255), 0.38, x=diag_x, limit=30)
            panel_text(207, f'CMD:{diag.get("command_dx", 0):+d},{diag.get("command_dy", 0):+d}', panel_color, 0.31, x=diag_x, limit=30)
            panel_text(228, f'QUEUE:{diag.get("queue_state", "EMPTY")} STEPS:{diag.get("queue_steps", 0)}', accent_color, 0.28, x=diag_x, limit=32)
            panel_text(249, f'OUTPUT:{diag.get("output_state", "IDLE")}', good_color if diag.get('output_state') == 'SENT' else warn_color, 0.31, x=diag_x, limit=30)
            panel_text(270, f'WHY:{diag.get("output_reason", "--")}', muted_color, 0.28, x=diag_x, limit=31)
            panel_text(291, f'LAST:{diag.get("last_dx", 0):+d},{diag.get("last_dy", 0):+d}', panel_color, 0.31, x=diag_x, limit=30)
            last_output_at = float(diag.get('last_output_at', 0.0) or 0.0)
            output_age_ms = (time.perf_counter() - last_output_at) * 1000.0 if last_output_at else -1.0
            panel_text(312, f'OUTPUT AGE:{output_age_ms:.0f}ms' if output_age_ms >= 0 else 'OUTPUT AGE:--', muted_color, 0.28, x=diag_x, limit=30)
            panel_text(342, f'TARGET ID:{diag.get("target_id") if diag.get("target_id") is not None else "NONE"}', accent_color, 0.30, x=diag_x, limit=30)
            worker_state = str(diag.get('worker_state', 'UNKNOWN'))
            panel_text(363, f'WORKER:{worker_state}', good_color if worker_state == 'RUNNING' else warn_color, 0.30, x=diag_x, limit=30)
            expiry_at = float(diag.get('last_command_expires_at', 0.0) or 0.0)
            expiry_ms = max(0.0, (expiry_at - time.perf_counter()) * 1000.0) if expiry_at else 0.0
            panel_text(384, f'CMD EXPIRES IN:{expiry_ms:.0f}ms', muted_color, 0.28, x=diag_x, limit=30)
            self._sample_resource_usage()
            ram_text = f'RAM:{self._process_ram_mb:.0f}MB' if self._process_ram_mb >= 0 else 'RAM:N/A'
            panel_text(170, f'RAW:{raw_state} {ram_text} VRAM:{self._cuda_allocated_mb:.0f}/{self._cuda_reserved_mb:.0f}MB', accent_color, 0.25, x=diag_x, limit=42)
            capture_short = 'DXGI' if self._dxcam_camera is not None else ('MSS' if self._mss_camera is not None else '--')
            panel_text(
                frame_h - 10,
                f'CAPTURE {capture_short} | FOV {self.aim_fov_size}px | '
                f'HB L{self.aim_hitbox_left_offset:+d} R{self.aim_hitbox_right_offset:+d} '
                f'T{self.aim_hitbox_top_offset:+d} B{self.aim_hitbox_bottom_offset:+d} | NUM1/2/3',
                accent_color, 0.27, x=12, limit=58,
            )
            cv2.line(panel, (0, 0), (0, frame_h), (70, 80, 95), 1, cv2.LINE_AA)
            debug_output = np.hstack((debug_frame, panel))

            if self.debug_mode:
                cv2.imshow(self.debug_window_name, debug_output)

            if self.debug_record_mode:
                record_frame = debug_output.copy()
                cv2.circle(record_frame, (record_frame.shape[1] - 24, 22), 6, (0, 0, 255), -1, cv2.LINE_AA)
                cv2.putText(
                    record_frame, 'REC',
                    (record_frame.shape[1] - 72, 46),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.40, (0, 0, 255), 1, cv2.LINE_AA,
                )
                cv2.imshow(self.debug_record_window_name, record_frame)
                self._write_debug_recording_frame(record_frame)

            cv2.waitKey(1)
        except cv2.error as exc:
            print(f'[ERROR] Debug preview failed: {exc}')
            if self.debug_mode:
                self.debug_mode = False
                self.debug_window_created = False
            if self.debug_record_mode:
                self._stop_debug_recording_view()

    @staticmethod
    def is_shooting():
        return win32api.GetKeyState(0x01) in (-127, -128)

    @staticmethod
    def crosshair_inside_box(x1, y1, x2, y2, center_x, center_y):
        return x1 <= center_x <= x2 and y1 <= center_y <= y2

    def _scaled_hitbox_bounds(self, x1, y1, x2, y2):
        """Return independently adjusted Aim Assist edges for one detection.

        Each offset is a percentage of the original box width/height. Negative
        moves that edge left/up; positive moves it right/down. The raw detector
        box is never changed, so identity association and friend/self filtering
        continue to use the original model coordinates.
        """
        raw_left, raw_right = sorted((float(x1), float(x2)))
        raw_top, raw_bottom = sorted((float(y1), float(y2)))
        box_width = max(1.0, raw_right - raw_left)
        box_height = max(1.0, raw_bottom - raw_top)

        left = raw_left + box_width * (float(self.aim_hitbox_left_offset) / 100.0)
        right = raw_right + box_width * (float(self.aim_hitbox_right_offset) / 100.0)
        top = raw_top + box_height * (float(self.aim_hitbox_top_offset) / 100.0)
        bottom = raw_bottom + box_height * (float(self.aim_hitbox_bottom_offset) / 100.0)

        # Keep a valid non-zero region even if the user crosses opposing edges.
        if right - left < 1.0:
            center_x = (left + right) * 0.5
            left, right = center_x - 0.5, center_x + 0.5
        if bottom - top < 1.0:
            center_y = (top + bottom) * 0.5
            top, bottom = center_y - 0.5, center_y + 0.5
        return left, top, right, bottom

    def _hitbox_anchor(self, x1, y1, x2, y2, reticle_x, reticle_y):
        """Return the nearest point on the resized hitbox to the reticle.

        When the reticle is inside the adjusted bounds, its exact position is
        returned so manual placement remains authoritative.
        """
        left, top, right, bottom = self._scaled_hitbox_bounds(x1, y1, x2, y2)
        rx, ry = float(reticle_x), float(reticle_y)
        inside = left <= rx <= right and top <= ry <= bottom
        return (
            min(right, max(left, rx)),
            min(bottom, max(top, ry)),
            'INSIDE' if inside else 'EDGE',
        )

    @staticmethod
    def _crosshair_inside_bounds(x1, y1, x2, y2, left_pct, right_pct,
                                 top_pct, bottom_pct, crosshair_x, crosshair_y):
        """Test inside an asymmetric hit region proportional to the detected box.

        Edge settings are percentages of the detected character box dimensions,
        not fixed screen pixels. This keeps the region proportional as a target
        appears larger or smaller with distance.
        """
        box_width = max(1.0, float(x2 - x1))
        box_height = max(1.0, float(y2 - y1))
        center_x = (float(x1) + float(x2)) / 2.0
        center_y = (float(y1) + float(y2)) / 2.0
        left_extent = box_width * max(0.0, min(200.0, float(left_pct))) / 100.0
        right_extent = box_width * max(0.0, min(200.0, float(right_pct))) / 100.0
        top_extent = box_height * max(0.0, min(200.0, float(top_pct))) / 100.0
        bottom_extent = box_height * max(0.0, min(200.0, float(bottom_pct))) / 100.0
        if left_extent + right_extent <= 0.0 or top_extent + bottom_extent <= 0.0:
            return False
        return (
            center_x - left_extent <= crosshair_x <= center_x + right_extent
            and center_y - top_extent <= crosshair_y <= center_y + bottom_extent
        )

    def _choose_fov_anchor(self, x1, y1, x2, y2, radius, preferred_part=None):
        """Return an Aim Assist anchor when a box intersects the configurable FOV."""
        left, top, right, bottom = self._scaled_hitbox_bounds(x1, y1, x2, y2)
        # Acquisition always respects the configured circle, regardless of
        # whether the visual ring is currently shown on the desktop.
        if self.aim_fov_size <= 0:
            return None
        active_radius = max(0.0, float(radius))
        if active_radius <= 0.0:
            return None
        center_x, center_y = self._fov_center_local()
        nearest_x = min(max(center_x, left), right)
        nearest_y = min(max(center_y, top), bottom)
        edge_distance = math.hypot(nearest_x - center_x, nearest_y - center_y)
        if edge_distance > active_radius:
            return None
        return ('hitbox', nearest_x, nearest_y)

    def stop_requested(self):
        if not self.stop_event_handle:
            return False
        result = ctypes.windll.kernel32.WaitForSingleObject(self.stop_event_handle, 0)
        return result == 0

    def _get_reticle_screen_position(self, detection_box):
        """Map the calibrated crop reticle to absolute desktop coordinates."""
        return (
            float(detection_box['left']) + float(self.crosshair_x),
            float(detection_box['top']) + float(self.crosshair_y),
        )

    def start(self):
        self._start_debug_hotkey_watcher()
        print('[INFO] Beginning hidden screen capture')
        primary_bounds = get_primary_monitor_bounds()
        primary_left, primary_top, primary_right, primary_bottom = primary_bounds
        self._capture_bounds = primary_bounds
        self._max_capture_size = max(
            primary_right - primary_left, primary_bottom - primary_top
        )
        self._resize_capture_for_fov()
        detection_box = self._make_detection_box(primary_bounds)
        self._update_fov_overlay(detection_box)
        print(
            '[INFO] FOV center = calibrated reticle + configured offset; primary bounds '
            f'{(primary_left, primary_top, primary_right, primary_bottom)}; '
            f"capture origin {(detection_box['left'], detection_box['top'])}; "
            f"capture size {detection_box['width']}x{detection_box['height']}; "
            f'calibrated crop point ({self.crosshair_x:.1f}, {self.crosshair_y:.1f}); '
            f'diameter {self.aim_fov_size}px acquisition=ON ring_visible={self.aim_fov_visible}; '
            f'offset=({self.aim_fov_offset_x},{self.aim_fov_offset_y})',
            flush=True,
        )
        try:
            self._initialize_capture_backend(detection_box, primary_bounds)
            self._raw_mouse_input_available = self._raw_mouse_input.start()
            if self._raw_mouse_input_available:
                print('[INFO] Physical mouse input reader ready for manual point selection.', flush=True)
            else:
                print(f'[WARN] Physical mouse input reader unavailable: {self._raw_mouse_input.error}', flush=True)
            self._start_aim_motion_worker()
            print('[INFO] Fresh mouse output worker started (one bounded delta per frame).', flush=True)
            while not self.stop_requested():
                loop_now = time.perf_counter()
                if self._runtime_prev_frame_at is not None:
                    loop_dt = max(0.001, loop_now - self._runtime_prev_frame_at)
                    loop_sample_fps = 1.0 / loop_dt
                    self._runtime_fps = (
                        loop_sample_fps if self._runtime_fps <= 0.0
                        else 0.82 * self._runtime_fps + 0.18 * loop_sample_fps
                    )
                self._runtime_prev_frame_at = loop_now
                self._refresh_menu_settings()
                resized_capture = self._resize_capture_for_fov()
                updated_box = self._make_detection_box(primary_bounds)
                box_changed = any(
                    int(updated_box[key]) != int(detection_box.get(key, -999999))
                    for key in ('left', 'top', 'width', 'height')
                )
                if resized_capture or box_changed:
                    detection_box = updated_box
                    self._camera_prev_gray = None
                    self._camera_motion_affine = None
                    self._reset_aim_tracking()
                else:
                    detection_box = updated_box
                self._process_activation_events()
                self.status_overlay.pump()
                self.update_debug_toggle()
                self.update_debug_crosshair_position()
                self._update_fov_overlay(detection_box)
                capture_started = time.perf_counter()
                initial_frame = self._grab_screen_frame(detection_box, primary_bounds)
                frame_captured_at = time.perf_counter()
                # DXGI returns BGRA and MSS fallback returns the same pixel layout;
                # keep a single downstream conversion and the existing YOLO input.
                frame = np.asarray(initial_frame, dtype=np.uint8)
                if frame.size == 0:
                    self.trigger_candidate_since = None
                    if self.trigger_held:
                        self.set_left_button(False)
                    continue

                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                self._last_capture_ms = (time.perf_counter() - capture_started) * 1000.0
                self._update_camera_motion(frame, frame_timestamp=frame_captured_at)
                manual_dx, manual_dy = self._raw_mouse_input.consume()
                self._manual_mouse_dx = manual_dx
                self._manual_mouse_dy = manual_dy
                if abs(manual_dx) + abs(manual_dy) >= 1:
                    self._manual_mouse_last_at = time.perf_counter()
                self._manual_mouse_active = bool(
                    self.manual_input_priority and self._raw_mouse_input_available
                    and self.master_enabled and self.aim_enabled
                    and (abs(manual_dx) + abs(manual_dy) >= 1)
                )
                packed_cpu = self._predict_detection_rows(frame)
                self._last_frame_age_ms = max(0.0, (time.perf_counter() - frame_captured_at) * 1000.0)
                postprocess_started = time.perf_counter()
                trigger_box_found = False
                boxes_cpu = packed_cpu[:, :4].astype(np.float32, copy=False) if len(packed_cpu) else np.empty((0, 4), dtype=np.float32)
                confidences_cpu = packed_cpu[:, 4].astype(np.float32, copy=False) if len(packed_cpu) else np.empty((0,), dtype=np.float32)
                class_ids_cpu = packed_cpu[:, 5].astype(np.int32, copy=False) if len(packed_cpu) else np.empty((0,), dtype=np.int32)
                own_player_flags = []
                best_aim_target = None
                aim_candidates = []
                aim_observations = []
                best_aim_distance = float('inf')
                trigger_mode_selected = self.trigger_enabled and self.master_enabled
                aim_mode_selected = self.aim_enabled and self.master_enabled

                # Friend/self appearance checks only matter for detections near
                # the active Aim FOV or inside the configured Trigger region.
                # Every detection is checked as soon as it can affect either feature.
                relevant_indices = []
                friend_radius = self._fov_radius(frame.shape) + 36.0
                for detection_index, coords in enumerate(boxes_cpu):
                    x1r, y1r, x2r, y2r = map(float, coords[:4])
                    if self._box_in_ignore_region((x1r, y1r, x2r, y2r), frame.shape):
                        continue
                    near_aim_region = self._choose_fov_anchor(
                        x1r, y1r, x2r, y2r, friend_radius
                    ) is not None
                    trigger_region = self._crosshair_inside_bounds(
                        x1r, y1r, x2r, y2r,
                        self.trigger_box_left, self.trigger_box_right,
                        self.trigger_box_top, self.trigger_box_bottom,
                        self.crosshair_x, self.crosshair_y,
                    )
                    if near_aim_region or trigger_region:
                        relevant_indices.append(detection_index)

                friend_flags = [False] * len(boxes_cpu)
                friend_reasons = ['DEFERRED OUTSIDE AIM REGION'] * len(boxes_cpu)
                if self._friend_capture_pending:
                    self._friend_capture_pending = False
                    friend_flags = self._capture_friend_references(frame, boxes_cpu)
                    friend_reasons = ['CAPTURED' if flag else '' for flag in friend_flags]
                elif self.friend_filter_enabled:
                    friend_flags, friend_reasons = self._classify_friend_detections(
                        frame, boxes_cpu, relevant_indices
                    )
                else:
                    friend_flags = [False] * len(boxes_cpu)
                    friend_reasons = ['FILTER OFF'] * len(boxes_cpu)

                recognized_target_ids = self._recognized_target_ids
                class_filter_active = bool(recognized_target_ids)

                valid_records = []
                own_player_flags = [True] * len(boxes_cpu)  # True means filtered in the Debug view.
                self._debug_detection_meta = [None] * len(boxes_cpu)
                # Local-avatar identity must be checked across the entire detector
                # crop; otherwise the player's box can be ignored only after it
                # has already entered the aim/trigger region. The crop is still
                # bounded to the Lunar capture region, keeping this pass lightweight.
                # Numpad 3 is now a spatial ignore region. Do not run the old
                # per-detection appearance classifier on legacy reference files;
                # it adds CPU work and is no longer part of the active workflow.
                self_flags = [False] * len(boxes_cpu)
                self_scores = [0.0] * len(boxes_cpu)
                # Keep the menu value as a user preference, but cap the
                # detector-side threshold at 14% so small/distant NPCs are not
                # discarded merely because an older profile stored 21-25%.
                minimum_confidence = max(
                    float(self._target_confidence_floor), min(float(self.conf), 0.14)
                )
                self._last_effective_confidence = minimum_confidence
                for detection_index, (x1f, y1f, x2f, y2f) in enumerate(boxes_cpu):
                    x1, y1, x2, y2 = int(x1f), int(y1f), int(x2f), int(y2f)
                    width = max(1, x2 - x1)
                    height = max(1, y2 - y1)
                    confidence_value = float(confidences_cpu[detection_index]) if detection_index < len(confidences_cpu) else 1.0
                    class_id = int(class_ids_cpu[detection_index]) if detection_index < len(class_ids_cpu) else 0
                    ratio = height / float(width)
                    # Small NPCs at distance and partially occluded bodies can
                    # legitimately produce narrow boxes; tracking confirmation
                    # below filters most one-frame false positives.
                    geometry_valid = (
                        height >= 4 and width >= 2 and width * height >= 8
                        and 0.35 <= ratio <= 9.0
                    )
                    class_valid = (not class_filter_active) or (class_id in recognized_target_ids)
                    confidence_valid = confidence_value >= minimum_confidence
                    valid = geometry_valid and class_valid and confidence_valid
                    is_friend = bool(
                        self.friend_filter_enabled
                        and detection_index < len(friend_flags)
                        and friend_flags[detection_index]
                    )
                    is_self = False  # Numpad 3 now uses a spatial ignore region, not identity matching.
                    in_ignore_region = self._box_in_ignore_region((x1f, y1f, x2f, y2f), frame.shape)
                    self_score = 0.0
                    filtered_reason = (
                        'IGNORE REGION' if in_ignore_region else
                        ('LOW CONF' if not confidence_valid else
                         ('CLASS' if not class_valid else ('SHAPE' if not geometry_valid else '')))
                    )
                    self._debug_detection_meta[detection_index] = {
                        'confidence': confidence_value,
                        'class_id': class_id,
                        'is_self': False,
                        'is_ignore_region': in_ignore_region,
                        'self_score': self_score,
                        'filtered_reason': filtered_reason,
                    }
                    if in_ignore_region:
                        continue
                    if not valid:
                        continue

                    own_player_flags[detection_index] = False
                    box = (float(x1), float(y1), float(x2), float(y2))
                    valid_records.append({
                        'index': detection_index,
                        'box': box,
                        'center': ((x1 + x2) / 2.0, (y1 + y2) / 2.0),
                        'confidence': confidence_value,
                        'class_id': class_id,
                        'is_friend': is_friend,
                    })

                self_boxes = [
                    tuple(float(value) for value in boxes_cpu[index][:4])
                    for index, flag in enumerate(self_flags) if flag
                ]
                if self_boxes:
                    self._exclude_self_from_tracking(self_boxes)
                # Timestamp observations at capture time, not inference completion.
                # Variable inference latency must not distort target velocity.
                valid_records = self._confirm_detection_tracks(valid_records, frame_captured_at)
                if (aim_mode_selected and self.head_model is not None
                        and (self.debug_mode or self.debug_record_mode)):
                    head_points = self._detect_head_points(frame, valid_records)
                else:
                    head_points = {}
                    self._head_inference_ms = 0.0
                self._debug_head_points = head_points
                self._last_head_raw_point = None
                self._last_head_corrected_point = None
                for record in valid_records:
                    detection_index = record['index']
                    x1, y1, x2, y2 = map(int, record['box'])
                    box_center_x, box_center_y = record['center']
                    is_friend = bool(record['is_friend'])
                    track_id = record.get('track_id')
                    confirmed = bool(record.get('confirmed', False))
                    if self._debug_detection_meta[detection_index] is not None:
                        self._debug_detection_meta[detection_index].update({
                            'track_id': track_id,
                            'confirmed': confirmed,
                        })

                    # Only confirmed person-like detections may trigger the bot.
                    if (trigger_mode_selected and confirmed and not is_friend
                        and self._crosshair_inside_bounds(
                            x1, y1, x2, y2,
                            self.trigger_box_left, self.trigger_box_right,
                            self.trigger_box_top, self.trigger_box_bottom,
                            self.crosshair_x, self.crosshair_y,
                        )):
                        trigger_box_found = True

                    if aim_mode_selected:
                        head_info = head_points.get(detection_index)
                        if self.aim_target_mode == 'head':
                            anchor_x, anchor_y = self._point_for_box(x1, y1, x2, y2)
                            anchor_mode = 'HEAD'
                        else:
                            anchor_x, anchor_y, anchor_mode = self._hitbox_anchor(
                                x1, y1, x2, y2, self.crosshair_x, self.crosshair_y
                            )
                        fov_cx, fov_cy = self._fov_center_local()
                        if self.aim_target_mode == 'head':
                            # HEAD mode tests the calibrated head point itself.
                            fov_anchor_x, fov_anchor_y = anchor_x, anchor_y
                        else:
                            # ALL mode tests the point on the resized hitbox that
                            # is nearest to the actual FOV center. The reticle may
                            # have an independent configured FOV offset.
                            fov_left, fov_top, fov_right, fov_bottom = self._scaled_hitbox_bounds(
                                x1, y1, x2, y2
                            )
                            fov_anchor_x = min(fov_right, max(fov_left, fov_cx))
                            fov_anchor_y = min(fov_bottom, max(fov_top, fov_cy))
                        fov_distance = math.hypot(fov_anchor_x - fov_cx, fov_anchor_y - fov_cy)
                        observation = {
                            'part': 'head-point' if self.aim_target_mode == 'head' else 'hitbox',
                            'x': anchor_x,
                            'y': anchor_y,
                            'anchor_mode': anchor_mode,
                            'distance': fov_distance,
                            'box': (x1, y1, x2, y2),
                            'box_center': (box_center_x, box_center_y),
                            'is_friend': is_friend,
                            'track_id': track_id,
                            'class_id': record.get('class_id'),
                            'confirmed': confirmed,
                            'confidence': record['confidence'],
                            'head_confidence': (head_info or {}).get('confidence', 0.0),
                            'head_source': (head_info or {}).get('source', 'DISABLED'),
                            'head_raw_point': (head_info or {}).get('raw_point'),
                            'head_corrected_point': (head_info or {}).get('point'),
                        }
                        # Existing locks can still match a briefly unconfirmed
                        # observation; new locks require two consistent frames.
                        aim_observations.append(observation)
                        aim_radius = self._fov_radius(frame.shape)
                        if not is_friend and confirmed and fov_distance <= aim_radius:
                            aim_candidates.append(observation)

                self._update_self_id_snapshot(frame, boxes_cpu, confidences_cpu, class_ids_cpu)

                if aim_mode_selected:
                    now_track = frame_captured_at
                    locked_observation = self._find_locked_aim_observation(
                        aim_observations, now_track
                    ) if self._aim_locked_box is not None else None

                    chosen = None
                    new_target = False
                    locked_track_id = self._aim_locked_track_id
                    locked_box_before_reset = self._aim_locked_box

                    # Another detection cannot steal the active target merely
                    # because it enters the FOV. A switch requires real mouse
                    # movement onto a different resized hitbox.
                    manual_alternate = self._pick_manual_retarget_candidate(
                        aim_candidates,
                        excluded_track_id=locked_track_id,
                        excluded_box=locked_box_before_reset,
                    ) if locked_box_before_reset is not None else None

                    if manual_alternate is not None:
                        self._reset_aim_tracking()
                        chosen = manual_alternate
                        new_target = True
                    elif locked_observation is not None:
                        if locked_observation.get('is_friend', False):
                            # Release a target identified as a friend. Do not
                            # immediately bind a different nearby detection.
                            self._reset_aim_tracking(require_manual_retarget=True)
                        else:
                            chosen = locked_observation
                    elif self._aim_locked_box is not None:
                        # Keep the selected identity during a brief detector gap,
                        # but do not estimate a future point and never move the
                        # mouse without a current detector-backed measurement.
                        if self._aim_lock_lost_since is None:
                            self._aim_lock_lost_since = now_track
                        lost_duration = now_track - self._aim_lock_lost_since
                        self._aim_lock_misses += 1
                        self._aim_track_state = 'WAIT DETECTION'
                        self._cancel_aim_motion('NO FRESH DETECTION')
                        if lost_duration > 0.72:
                            # The old lock has genuinely expired. Release its identity,
                            # but return to normal FOV acquisition on the next frame.
                            # Requiring a physical mouse movement here left the Aim Assist
                            # stuck in WAIT MANUAL after a target exited the FOV.
                            self._reset_aim_tracking(require_manual_retarget=False)

                    # Initial acquisition is allowed only when no previous lock
                    # remains. Alternative detections cannot steal a held lock.
                    if chosen is None and aim_candidates and self._aim_locked_box is None:
                        if self._aim_lock_wait_manual_retarget:
                            # After a true lock loss, reacquire only when the
                            # physical mouse moves onto a valid resized hitbox.
                            chosen = self._pick_manual_retarget_candidate(aim_candidates)
                            if chosen is not None:
                                self._aim_lock_wait_manual_retarget = False
                                new_target = True
                        else:
                            chosen = self._pick_initial_aim_candidate(aim_candidates)
                            new_target = chosen is not None

                    if chosen is not None:
                        # A target coordinate is produced only from a fresh
                        # detector observation. There is no predicted aim point.
                        best_aim_target = self._update_locked_aim_measurement(
                            chosen, now_track, new_target=new_target
                        )
                    else:
                        if self._aim_locked_box is not None:
                            self._aim_track_state = 'WAIT DETECTION'
                        else:
                            self._aim_track_state = (
                                'WAIT MANUAL' if self._aim_lock_wait_manual_retarget else 'IDLE'
                            )
                        best_aim_target = None

                    if best_aim_target is not None:
                        self._last_head_raw_point = best_aim_target.get('head_raw_point')
                        self._last_head_corrected_point = best_aim_target.get('head_corrected_point')
                    else:
                        self._last_head_raw_point = None
                        self._last_head_corrected_point = None
                else:
                    self._reset_aim_tracking()
                    self._last_head_raw_point = None
                    self._last_head_corrected_point = None

                # W2.0 TEST: only allow actual Aim Assist movement while the
                # user physically holds LMB. Ignore our own Trigger Bot press,
                # otherwise the Trigger Bot could activate the Aim Assist itself.
                physical_lmb_down = bool(win32api.GetAsyncKeyState(0x01) & 0x8000)
                aim_input_allowed = (
                    not self.aim_hold_lmb
                    or (physical_lmb_down and not self.trigger_held)
                )
                aim_block_reason = ''
                target_inside_fov = False
                if best_aim_target is not None:
                    target_px = best_aim_target.get('x', self._filtered_aim_x)
                    target_py = best_aim_target.get('y', self._filtered_aim_y)
                    target_inside_fov = self._aim_point_inside_fov(
                        target_px, target_py, frame.shape
                    )

                if not self.master_enabled:
                    aim_block_reason = 'MASTER OFF'
                elif not self.aim_enabled:
                    aim_block_reason = 'AIM DISABLED'
                elif best_aim_target is None:
                    aim_block_reason = 'NO VALID TARGET'
                elif not target_inside_fov:
                    # Strict boundary: no movement is sent outside the user's FOV.
                    aim_block_reason = 'OUTSIDE FOV'
                elif self._filtered_aim_x is None or self._filtered_aim_y is None:
                    aim_block_reason = 'NO FILTERED POINT'
                elif not aim_input_allowed:
                    aim_block_reason = 'HOLD LMB REQUIRED'

                if not aim_block_reason:
                    # Physical mouse movement updates the normalized point inside
                    # the hitbox; it no longer disables the controller for that
                    # frame. This lets manual camera movement and tracking coexist.
                    # best_aim_target can only come from a fresh detector
                    # observation; no prediction-weighted control path exists.
                    tracking_weight = 1.0
                    self._apply_aim_assist(
                        self._filtered_aim_x,
                        self._filtered_aim_y,
                        tracking_weight=tracking_weight,
                    )

                if aim_block_reason:
                    self._set_aim_diagnostics(
                        decision='BLOCKED', reason=aim_block_reason,
                        target_id=(best_aim_target.get('track_id') if best_aim_target else None),
                        tracking_weight=0.0, command_dx=0, command_dy=0,
                    )
                    self._aim_remainder_x = 0.0
                    self._aim_remainder_y = 0.0
                    self._aim_last_update_time = None
                    self._stable_prev_error_x = None
                    self._stable_prev_error_y = None
                    self._reset_new_controller_state()
                    self._cancel_aim_motion(aim_block_reason)

                self._last_postprocess_ms = (time.perf_counter() - postprocess_started) * 1000.0
                self.show_debug_frame(
                    frame, boxes_cpu, own_player_flags, trigger_box_found,
                    best_aim_target, friend_flags, friend_reasons,
                )
                should_hold = (
                    self.master_enabled and self.trigger_enabled and trigger_box_found
                )
                if should_hold:
                    if self.trigger_candidate_since is None:
                        self.trigger_candidate_since = time.perf_counter()
                    elapsed_ms = (time.perf_counter() - self.trigger_candidate_since) * 1000.0
                    if (
                        elapsed_ms >= self.trigger_delay_ms
                        and not self.trigger_held
                        and not Aimbot.is_shooting()
                    ):
                        self.set_left_button(True)
                else:
                    self.trigger_candidate_since = None
                    if self.trigger_held:
                        self.set_left_button(False)
        finally:
            self._stop_aim_motion_worker()
            self._raw_mouse_input.stop()
            self._raw_mouse_input_available = False
            self._manual_mouse_active = False
            self._stop_head_inference_worker()
            self.trigger_candidate_since = None
            if self.trigger_held:
                self.set_left_button(False)
            self._stop_activation_watcher()
            self._stop_debug_hotkey_watcher()
            if self.self_id_mode or self.self_id_window_created:
                self._stop_self_id_window()
            if self.debug_record_mode or self._debug_recording_writer is not None:
                self._stop_debug_recording_view()
            if self.debug_mode:
                try:
                    cv2.destroyWindow(self.debug_window_name)
                except cv2.error:
                    pass
                self.debug_mode = False
                self.debug_window_created = False
            self.status_overlay.close()
            if self.aim_fov_overlay is not None:
                self.aim_fov_overlay.close()
                self.aim_fov_overlay = None
            if self.stop_event_handle:
                ctypes.windll.kernel32.CloseHandle(self.stop_event_handle)
                self.stop_event_handle = None
            self._close_capture_backend()
            cv2.destroyAllWindows()

    @staticmethod
    def clean_up():
        print('\n[INFO] QUITTING...')
        instance = Aimbot.active_instance
        if instance is not None:
            instance._stop_aim_motion_worker()
            instance._stop_head_inference_worker()
            if instance.trigger_held:
                instance.set_left_button(False)
            instance._stop_activation_watcher()
            instance._stop_debug_hotkey_watcher()
            if instance.self_id_mode or instance.self_id_window_created:
                instance._stop_self_id_window()
            if instance.debug_record_mode or instance._debug_recording_writer is not None:
                instance._stop_debug_recording_view()
            instance.status_overlay.close()
            if instance.aim_fov_overlay is not None:
                instance.aim_fov_overlay.close()
                instance.aim_fov_overlay = None
        try:
            if instance is not None:
                instance._close_capture_backend()
        finally:
            cv2.destroyAllWindows()
        os._exit(0)


if __name__ == '__main__':
    print('You are in the wrong directory and are running the wrong file; you must run lunar.py')
