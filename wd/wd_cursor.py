"""把系统鼠标指针合成进抓屏画面（DXGI 桌面复制本身不含指针）。

指针位图按 HCURSOR 句柄缓存，每帧只做一次 GetCursorInfo + 小图 alpha 混合，
1080p 下开销约 0.05ms。
"""
import ctypes
import time
from ctypes import wintypes

import numpy as np

CURSOR_SHOWING = 0x0001
DIB_RGB_COLORS = 0
BI_RGB = 0

_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None
_gdi32 = ctypes.windll.gdi32 if hasattr(ctypes, "windll") else None


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", ctypes.c_void_p)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


_cache = {"handle": None, "sprite": None, "hot": (0, 0), "at": 0.0}

if _user32 and _gdi32:
    _HANDLE = ctypes.c_void_p
    _user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
    _user32.GetIconInfo.argtypes = [_HANDLE, ctypes.POINTER(ICONINFO)]
    _user32.GetDC.argtypes = [_HANDLE]
    _user32.GetDC.restype = _HANDLE
    _user32.ReleaseDC.argtypes = [_HANDLE, _HANDLE]
    _gdi32.GetObjectW.argtypes = [_HANDLE, ctypes.c_int, ctypes.c_void_p]
    _gdi32.CreateCompatibleDC.argtypes = [_HANDLE]
    _gdi32.CreateCompatibleDC.restype = _HANDLE
    _gdi32.SelectObject.argtypes = [_HANDLE, _HANDLE]
    _gdi32.SelectObject.restype = _HANDLE
    _gdi32.GetDIBits.argtypes = [_HANDLE, _HANDLE, wintypes.UINT, wintypes.UINT,
                                 ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    _gdi32.DeleteDC.argtypes = [_HANDLE]
    _gdi32.DeleteObject.argtypes = [_HANDLE]


def _read_sprite(handle):
    """读取 HCURSOR 的 32 位 BGRA 位图（含热点）。"""
    info = ICONINFO()
    if not _user32.GetIconInfo(handle, ctypes.byref(info)):
        return None
    try:
        if not info.hbmColor:
            return None  # 老式单色指针，直接用掩码太复杂，跳过
        bitmap = BITMAP()
        if not _gdi32.GetObjectW(info.hbmColor, ctypes.sizeof(BITMAP), ctypes.byref(bitmap)):
            return None
        width, height = int(bitmap.bmWidth), int(bitmap.bmHeight)
        if not (0 < width <= 256 and 0 < height <= 256):
            return None
        screen_dc = _user32.GetDC(None)
        mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
        old = _gdi32.SelectObject(mem_dc, info.hbmColor)
        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB
        buffer = (ctypes.c_ubyte * (width * height * 4))()
        got = _gdi32.GetDIBits(mem_dc, info.hbmColor, 0, height, buffer,
                               ctypes.byref(header), DIB_RGB_COLORS)
        _gdi32.SelectObject(mem_dc, old)
        _gdi32.DeleteDC(mem_dc)
        _user32.ReleaseDC(None, screen_dc)
        if not got:
            return None
        sprite = np.frombuffer(buffer, dtype=np.uint8).reshape(height, width, 4).copy()
        if int(sprite[:, :, 3].max()) == 0:
            return None  # 没有 alpha 信息的旧指针，避免画成黑块
        return sprite, (int(info.xHotspot), int(info.yHotspot))
    finally:
        if info.hbmColor:
            _gdi32.DeleteObject(info.hbmColor)
        if info.hbmMask:
            _gdi32.DeleteObject(info.hbmMask)


def _cursor_sprite():
    info = CURSORINFO()
    info.cbSize = ctypes.sizeof(CURSORINFO)
    if not _user32.GetCursorInfo(ctypes.byref(info)):
        return None
    if not (info.flags & CURSOR_SHOWING) or not info.hCursor:
        return None
    now = time.monotonic()
    if _cache["handle"] == info.hCursor and _cache["sprite"] is not None:
        if now - _cache["at"] < 2.0:
            return _cache["sprite"], _cache["hot"], info.ptScreenPos
    parsed = _read_sprite(info.hCursor)
    if parsed is None:
        _cache.update(handle=info.hCursor, sprite=None, hot=(0, 0), at=now)
        return None
    _cache.update(handle=info.hCursor, sprite=parsed[0], hot=parsed[1], at=now)
    return parsed[0], parsed[1], info.ptScreenPos


def draw(frame: np.ndarray) -> bool:
    """把当前鼠标指针混合到 RGB 画面上（原地修改）。"""
    if _user32 is None or frame is None or frame.ndim != 3 or frame.shape[2] != 3:
        return False
    state = _cursor_sprite()
    if state is None:
        return False
    sprite, (hot_x, hot_y), point = state
    height, width = frame.shape[0], frame.shape[1]
    left = int(point.x) - hot_x
    top = int(point.y) - hot_y
    x0, y0 = max(0, left), max(0, top)
    x1 = min(width, left + sprite.shape[1])
    y1 = min(height, top + sprite.shape[0])
    if x1 <= x0 or y1 <= y0:
        return False
    part = sprite[y0 - top:y1 - top, x0 - left:x1 - left]
    alpha = (part[:, :, 3:4].astype(np.uint16) + 1) / 256.0
    target = frame[y0:y1, x0:x1]
    src = part[:, :, 2::-1].astype(np.float32)   # BGRA -> RGB
    blended = src * alpha + target.astype(np.float32) * (1.0 - alpha)
    target[:] = blended.astype(np.uint8)
    return True
