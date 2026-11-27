"""Windows SendInput 注入；仅在远程窗口获得焦点时调用。"""
import ctypes
from ctypes import wintypes

LONG = ctypes.c_long
DWORD = wintypes.DWORD
WORD = wintypes.WORD
ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
KEYEVENTF_KEYUP = 0x0002


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", LONG), ("dy", LONG), ("mouseData", DWORD),
                ("dwFlags", DWORD), ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", WORD), ("wScan", WORD), ("dwFlags", DWORD),
                ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", DWORD), ("u", _INPUTUNION)]


_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None
if _user32:
    _user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    _user32.SendInput.restype = wintypes.UINT


def _send(*inputs):
    if not _user32:
        return 0
    arr = (INPUT * len(inputs))(*inputs)
    return _user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def screen_size():
    if not _user32:
        return 1920, 1080
    return _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)


def mouse_move(nx: float, ny: float):
    sw, sh = screen_size()
    i = INPUT(type=INPUT_MOUSE)
    i.mi.dx = int(max(0.0, min(1.0, nx)) * 65535)
    i.mi.dy = int(max(0.0, min(1.0, ny)) * 65535)
    i.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
    return _send(i)


def mouse_button(button: str, down: bool):
    flags = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }.get(button)
    if not flags:
        return 0
    i = INPUT(type=INPUT_MOUSE)
    i.mi.dwFlags = flags[0] if down else flags[1]
    return _send(i)


def mouse_wheel(delta: int):
    i = INPUT(type=INPUT_MOUSE)
    i.mi.mouseData = DWORD(int(delta) & 0xFFFFFFFF)
    i.mi.dwFlags = MOUSEEVENTF_WHEEL
    return _send(i)


def key(vk: int, down: bool):
    if not vk:
        return 0
    i = INPUT(type=INPUT_KEYBOARD)
    i.ki.wVk = WORD(vk & 0xFF)
    i.ki.dwFlags = 0 if down else KEYEVENTF_KEYUP
    return _send(i)


def inject(event: dict):
    action = event.get("a")
    if action == "move":
        return mouse_move(float(event.get("nx", 0)), float(event.get("ny", 0)))
    if action == "down":
        return mouse_button(event.get("button", "left"), True)
    if action == "up":
        return mouse_button(event.get("button", "left"), False)
    if action == "wheel":
        return mouse_wheel(int(event.get("delta", 0)))
    if action == "key":
        return key(int(event.get("vk", 0)), bool(event.get("down", True)))
    if action == "reset":
        for b in ("left", "middle", "right"):
            mouse_button(b, False)
        for vk in (0x10, 0x11, 0x12, 0x1B):
            key(vk, False)
        return 1
    return 0


def qt_key_to_vk(key: int, text: str = "") -> int:
    from PySide6.QtCore import Qt
    special = {
        Qt.Key_Escape: 0x1B, Qt.Key_Tab: 0x09, Qt.Key_Backspace: 0x08,
        Qt.Key_Return: 0x0D, Qt.Key_Enter: 0x0D, Qt.Key_Shift: 0x10,
        Qt.Key_Control: 0x11, Qt.Key_Alt: 0x12, Qt.Key_Meta: 0x5B,
        Qt.Key_CapsLock: 0x14, Qt.Key_Space: 0x20, Qt.Key_PageUp: 0x21,
        Qt.Key_PageDown: 0x22, Qt.Key_End: 0x23, Qt.Key_Home: 0x24,
        Qt.Key_Left: 0x25, Qt.Key_Up: 0x26, Qt.Key_Right: 0x27, Qt.Key_Down: 0x28,
        Qt.Key_Insert: 0x2D, Qt.Key_Delete: 0x2E,
        Qt.Key_F1: 0x70, Qt.Key_F2: 0x71, Qt.Key_F3: 0x72, Qt.Key_F4: 0x73,
        Qt.Key_F5: 0x74, Qt.Key_F6: 0x75, Qt.Key_F7: 0x76, Qt.Key_F8: 0x77,
        Qt.Key_F9: 0x78, Qt.Key_F10: 0x79, Qt.Key_F11: 0x7A, Qt.Key_F12: 0x7B,
        Qt.Key_Semicolon: 0xBA, Qt.Key_Equal: 0xBB, Qt.Key_Comma: 0xBC,
        Qt.Key_Minus: 0xBD, Qt.Key_Period: 0xBE, Qt.Key_Slash: 0xBF,
        Qt.Key_BracketLeft: 0xDB, Qt.Key_Backslash: 0xDC, Qt.Key_BracketRight: 0xDD,
        Qt.Key_QuoteLeft: 0xC0,
    }
    if key in special:
        return special[key]
    if text and len(text) == 1:
        ch = text.upper()
        if "A" <= ch <= "Z" or "0" <= ch <= "9":
            return ord(ch)
    return 0
