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
DI_NORMAL = 0x0003

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
    _gdi32.CreateDIBSection.argtypes = [_HANDLE, ctypes.c_void_p, wintypes.UINT,
                                        ctypes.POINTER(ctypes.c_void_p), _HANDLE,
                                        wintypes.DWORD]
    _gdi32.CreateDIBSection.restype = _HANDLE
    _gdi32.GdiFlush.argtypes = []
    _user32.DrawIconEx.argtypes = [_HANDLE, ctypes.c_int, ctypes.c_int, _HANDLE,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT,
                                   _HANDLE, wintypes.UINT]
    _gdi32.DeleteDC.argtypes = [_HANDLE]
    _gdi32.DeleteObject.argtypes = [_HANDLE]


def _draw_on(handle, width, height, background):
    """把光标画到指定背景色的 32 位 DIB 上，返回 BGRA 数组。"""
    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = width
    header.biHeight = -height
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = BI_RGB
    bits = ctypes.c_void_p()
    screen_dc = _user32.GetDC(None)
    dib = _gdi32.CreateDIBSection(screen_dc, ctypes.byref(header), DIB_RGB_COLORS,
                                  ctypes.byref(bits), None, 0)
    if not dib or not bits:
        _user32.ReleaseDC(None, screen_dc)
        return None
    mem_dc = None
    try:
        ctypes.memset(bits, background, width * height * 4)
        mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
        _gdi32.SelectObject(mem_dc, dib)
        if not _user32.DrawIconEx(mem_dc, 0, 0, handle, width, height,
                                  0, None, DI_NORMAL):
            return None
        _gdi32.GdiFlush()
        return np.ctypeslib.as_array(
            ctypes.cast(bits, ctypes.POINTER(ctypes.c_ubyte * (width * height * 4))).contents
        ).reshape(height, width, 4).copy()
    finally:
        if mem_dc:
            _gdi32.DeleteDC(mem_dc)
        _gdi32.DeleteObject(dib)
        _user32.ReleaseDC(None, screen_dc)


def _read_sprite(handle):
    """读取 HCURSOR 位图（含热点）。

    在纯黑 / 纯白两种背景上各画一次再推导 alpha：单色光标、缺少 alpha 通道的
    早期光标、带 alpha 的现代光标都能正确处理，不会再出现"指针整个消失"。
    """
    info = ICONINFO()
    if not _user32.GetIconInfo(handle, ctypes.byref(info)):
        return None
    try:
        source = info.hbmColor or info.hbmMask
        if not source:
            return None
        bitmap = BITMAP()
        if not _gdi32.GetObjectW(source, ctypes.sizeof(BITMAP), ctypes.byref(bitmap)):
            return None
        width = int(bitmap.bmWidth)
        # 单色光标的掩码位图是 2 倍高（上半 AND、下半 XOR）
        height = int(bitmap.bmHeight)
        if not info.hbmColor:
            height //= 2
        if not (0 < width <= 256 and 0 < height <= 256):
            return None
        on_black = _draw_on(handle, width, height, 0)
        if on_black is None:
            return None
        on_white = _draw_on(handle, width, height, 255)
        if on_white is None:
            return None
        black = on_black[:, :, :3].astype(np.int16)
        white = on_white[:, :, :3].astype(np.int16)
        # 背景可见的地方两次绘制结果不同 → 该处透明
        alpha = np.clip(255 - (white - black).max(axis=2), 0, 255).astype(np.uint8)
        sprite = np.zeros((height, width, 4), dtype=np.uint8)
        sprite[:, :, 3] = alpha
        opaque = alpha > 0
        if opaque.any():
            value = on_black[:, :, :3].astype(np.float32) * 255.0 / np.maximum(alpha, 1)[..., None]
            sprite[:, :, :3] = np.clip(value, 0, 255).astype(np.uint8)
        if not opaque.any():
            return None
        return sprite, (int(info.xHotspot), int(info.yHotspot))
    finally:
        if info.hbmColor:
            _gdi32.DeleteObject(info.hbmColor)
        if info.hbmMask:
            _gdi32.DeleteObject(info.hbmMask)


def _sprite_cached(handle):
    now = time.monotonic()
    if _cache["handle"] == handle and _cache["sprite"] is not None:
        if now - _cache["at"] < 2.0:
            return _cache["sprite"], _cache["hot"]
    parsed = _read_sprite(handle)
    if parsed is None:
        _cache.update(handle=handle, sprite=None, hot=(0, 0), at=now)
        return None
    _cache.update(handle=handle, sprite=parsed[0], hot=parsed[1], at=now)
    return parsed[0], parsed[1]


def state():
    """给主控端用的指针状态：返回 (handle, sprite, hotspot, pos, visible)。

    sprite 是 BGRA 数组（读不到时为 None），pos 为屏幕坐标。主控端据此在本机
    按原生分辨率画指针，不再把指针混进画面里被缩放/压缩糊掉。
    """
    if _user32 is None:
        return None
    info = CURSORINFO()
    info.cbSize = ctypes.sizeof(CURSORINFO)
    if not _user32.GetCursorInfo(ctypes.byref(info)):
        return None
    point = (int(info.ptScreenPos.x), int(info.ptScreenPos.y))
    visible = bool(info.flags & CURSOR_SHOWING)
    handle = int(info.hCursor or 0)
    if not visible or not handle:
        return handle, None, (0, 0), point, False
    parsed = _sprite_cached(handle)
    if parsed is None:
        return handle, None, (0, 0), point, True
    return handle, parsed[0], parsed[1], point, True


def encode_png(sprite) -> bytes:
    """BGRA 指针位图 → PNG（保留 alpha），32x32 通常不到 1KB。"""
    try:
        import cv2
        ok, buf = cv2.imencode(".png", sprite)
        return buf.tobytes() if ok else b""
    except Exception:
        return b""


def _cursor_sprite():
    info = CURSORINFO()
    info.cbSize = ctypes.sizeof(CURSORINFO)
    if not _user32.GetCursorInfo(ctypes.byref(info)):
        return None
    if not (info.flags & CURSOR_SHOWING) or not info.hCursor:
        return None
    parsed = _sprite_cached(info.hCursor)
    if parsed is None:
        return None
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
