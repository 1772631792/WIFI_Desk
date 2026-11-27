"""低级键盘钩子：远程窗口激活时把按键全部截获，不再漏给本机。

Windows 的 Win 键、Alt+Tab、Ctrl+Shift+Esc 这类组合由系统直接处理，
Qt 的 keyPressEvent 根本收不到，所以只能挂 WH_KEYBOARD_LL。
钩子回调运行在钩子线程里，只做"解码 + 入队"，实际发送交给界面线程。

安全性：钩子只在本进程存活期间有效；窗口失焦立即停用，鼠标仍可正常操作，
所以不会把用户锁死在远程窗口里。
"""
import ctypes
import threading
from ctypes import wintypes

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_QUIT = 0x0012
LLKHF_EXTENDED = 0x01

_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None
_kernel32 = ctypes.windll.kernel32 if hasattr(ctypes, "windll") else None


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


if _user32:
    _HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                   wintypes.WPARAM, wintypes.LPARAM)
    _user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC,
                                          wintypes.HINSTANCE, wintypes.DWORD]
    _user32.SetWindowsHookExW.restype = ctypes.c_void_p
    _user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                       wintypes.WPARAM, wintypes.LPARAM]
    _user32.CallNextHookEx.restype = ctypes.c_ssize_t
    _user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    _user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                           wintypes.WPARAM, wintypes.LPARAM]
    _kernel32.GetModuleHandleW.restype = ctypes.c_void_p


class KeyboardGrab:
    """把系统键盘事件截下来交给回调，返回 True 表示吞掉该事件。"""

    def __init__(self, on_key):
        self._on_key = on_key
        self._active = threading.Event()
        self._thread = None
        self._hook = None
        self._tid = 0
        self._proc = None
        self._ready = threading.Event()

    @property
    def active(self) -> bool:
        return self._active.is_set()

    def start(self, timeout: float = 2.0) -> bool:
        if _user32 is None or self._thread is not None:
            return bool(self._thread)
        self._thread = threading.Thread(target=self._run, daemon=True, name="wd-keyhook")
        self._thread.start()
        self._ready.wait(timeout)
        return self._hook is not None

    def set_active(self, active: bool):
        if active:
            self._active.set()
        else:
            self._active.clear()

    def stop(self):
        self._active.clear()
        if _user32 and self._tid:
            try:
                _user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            except Exception:
                pass
        thread, self._thread = self._thread, None
        if thread and thread.is_alive():
            thread.join(timeout=0.5)

    def _run(self):
        proc_ref = None
        try:
            self._tid = int(_kernel32.GetCurrentThreadId())
            module = _kernel32.GetModuleHandleW(None)

            def proc(code, wparam, lparam):
                if code == 0 and self._active.is_set():
                    try:
                        info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                        down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                        if wparam in (WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP):
                            self._on_key(int(info.vkCode), down,
                                         bool(info.flags & LLKHF_EXTENDED))
                            return 1        # 吞掉：本机任何程序都收不到
                    except Exception:
                        pass
                return _user32.CallNextHookEx(None, code, wparam, lparam)

            proc_ref = _HOOKPROC(proc)
            self._proc = proc_ref
            self._hook = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc_ref, module, 0)
            self._ready.set()
            if not self._hook:
                return
            msg = wintypes.MSG()
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self._hook:
                try:
                    _user32.UnhookWindowsHookEx(self._hook)
                except Exception:
                    pass
                self._hook = None
            self._ready.set()
            _ = proc_ref   # 保持引用，避免回调对象被回收
