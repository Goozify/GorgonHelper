"""Global keyboard hotkey via Win32 WH_KEYBOARD_LL hook.

Same low-level hook approach as InventoryClickWatcher (WH_MOUSE_LL).
Must be started from the thread that runs the Qt/Windows message pump
(i.e. the qasync main thread).

Modifier bitmask (use | to combine):
    MOD_SHIFT = 1
    MOD_CTRL  = 2
    MOD_ALT   = 4
"""
import ctypes
import ctypes.wintypes as wt
from ctypes import CFUNCTYPE, c_int, c_longlong, POINTER

WH_KEYBOARD_LL = 13
WM_KEYDOWN     = 0x0100
WM_SYSKEYDOWN  = 0x0104   # sent when Alt is held + key pressed
HC_ACTION      = 0

VK_SHIFT   = 0x10
VK_CONTROL = 0x11
VK_MENU    = 0x12   # Alt

MOD_SHIFT = 1
MOD_CTRL  = 2
MOD_ALT   = 4

_GetForegroundWindow = ctypes.windll.user32.GetForegroundWindow
_GetWindowTextW      = ctypes.windll.user32.GetWindowTextW


def _foreground_window_title() -> str:
    """Return the title of the currently active (foreground) window."""
    hwnd = _GetForegroundWindow()
    buf  = ctypes.create_unicode_buffer(256)
    _GetWindowTextW(hwnd, buf, 256)
    return buf.value


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode",      wt.DWORD),
        ("scanCode",    wt.DWORD),
        ("flags",       wt.DWORD),
        ("time",        wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


HOOKPROC = CFUNCTYPE(c_longlong, c_int, wt.WPARAM, POINTER(KBDLLHOOKSTRUCT))

_gks = ctypes.windll.user32.GetAsyncKeyState


def _modifiers_match(required_mods: int) -> bool:
    """Return True if the current modifier state matches *required_mods*."""
    shift_held = bool(_gks(VK_SHIFT)   & 0x8000)
    ctrl_held  = bool(_gks(VK_CONTROL) & 0x8000)
    alt_held   = bool(_gks(VK_MENU)    & 0x8000)
    return (
        shift_held == bool(required_mods & MOD_SHIFT) and
        ctrl_held  == bool(required_mods & MOD_CTRL)  and
        alt_held   == bool(required_mods & MOD_ALT)
    )


class KeyboardHotkey:
    """Fires *callback* (no args) whenever *vk_code* is pressed globally,
    optionally requiring modifier keys via *modifiers* bitmask.

    Usage::

        hotkey = KeyboardHotkey(0x75, my_callback)            # F6
        hotkey = KeyboardHotkey(0x41, my_callback, MOD_CTRL)  # Ctrl+A
        hotkey.start()
        # ... later ...
        hotkey.stop()
    """

    def __init__(self, vk_code: int, callback, modifiers: int = 0,
                 active_window_contains: str = ""):
        self._vk   = vk_code
        self._mods = modifiers
        self._callback  = callback
        self._hook      = None
        self._proc_ref  = None   # keep reference so ctypes doesn't GC the function
        # When non-empty, the hotkey only fires if the foreground window title
        # contains this string (case-insensitive).  Keeps the key working
        # normally in all other applications.
        self._window_filter = active_window_contains.lower()

    # ------------------------------------------------------------------

    def start(self):
        if self._hook:
            return

        def _low_level_proc(nCode, wParam, lParam):
            if nCode == HC_ACTION and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                try:
                    if (lParam.contents.vkCode == self._vk and
                            _modifiers_match(self._mods)):
                        # Only fire when the configured window is in the foreground
                        if (not self._window_filter or
                                self._window_filter in _foreground_window_title().lower()):
                            self._callback()
                except Exception:
                    pass
            return ctypes.windll.user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._proc_ref = HOOKPROC(_low_level_proc)
        self._hook = ctypes.windll.user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._proc_ref, None, 0
        )

    def stop(self):
        if self._hook:
            ctypes.windll.user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
            self._proc_ref = None

    def update_vk(self, vk_code: int):
        """Change the trigger key without restarting the hook."""
        self._vk = vk_code

    def update_modifiers(self, mods: int):
        """Change the required modifier mask without restarting the hook."""
        self._mods = mods
