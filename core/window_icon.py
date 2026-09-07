"""Set native window icons used by the Windows taskbar, independently of Qt."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _icons(icon_path: str) -> tuple[int, int]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                 wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.LoadImageW.restype = wintypes.HANDLE
    small = user32.LoadImageW(None, icon_path, 1, user32.GetSystemMetrics(49),
                             user32.GetSystemMetrics(50), 0x10)
    large = user32.LoadImageW(None, icon_path, 1, user32.GetSystemMetrics(11),
                             user32.GetSystemMetrics(12), 0x10)
    if not small or not large:
        user32.DestroyIcon.argtypes = [wintypes.HANDLE]
        for handle in (small, large):
            if handle:
                user32.DestroyIcon(handle)
        raise ctypes.WinError(ctypes.get_last_error())
    # WM_SETICON retains these handles, it does not copy them. Keep this one
    # pair alive across hide/show and HWND recreation; Windows frees on exit.
    return small, large


def apply_window_icon(hwnd: int, icon_path: str | Path) -> None:
    """Populate ICON_SMALL/ICON_BIG on the actual HWND before taskbar display."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT,
        wintypes.WPARAM, wintypes.LPARAM, wintypes.UINT, wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t)]
    user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    small, large = _icons(str(Path(icon_path).resolve()))
    for kind, handle in ((0, small), (1, large)):
        previous = ctypes.c_size_t()
        if not user32.SendMessageTimeoutW(hwnd, 0x80, kind, handle, 2, 500, ctypes.byref(previous)):
            raise ctypes.WinError(ctypes.get_last_error())
