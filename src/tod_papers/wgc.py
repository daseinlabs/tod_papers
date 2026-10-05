"""Windows.Graphics.Capture (WGC) of one window, via the `windows-capture` package.

WGC captures the window's own DWM surface, so the pixels are the window's even when other windows cover it
(occlusion-independent) and work for Unity/DirectX swap chains (PrintWindow returns black on this build).
A free-threaded capture session pushes frames into a callback; we keep the latest one (BGRA, copied) and hand it
out on demand. Frames only arrive when the window content changes; a minimized window delivers none, so the
last frame stays available.

Used by tools/stage.py (live mirror of the game, cursor included) and by io_win.Grabber when TOD_GRAB=wgc
(the loop's screenshot, no cursor, cropped to the client area).
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

import numpy as np

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_dwm = ctypes.WinDLL("dwmapi")
DWMWA_EXTENDED_FRAME_BOUNDS = 9


def frame_bounds(hwnd: int) -> tuple[int, int, int, int]:
    """DWM extended frame bounds (screen px, l,t,r,b): the rect a WGC window capture covers."""
    r = wintypes.RECT()
    if _dwm.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r),
                                  ctypes.sizeof(r)) != 0:
        _u32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def client_in_frame(hwnd: int) -> tuple[int, int, int, int]:
    """Client area (x, y, w, h) relative to the captured frame's top-left."""
    l, t, _, _ = frame_bounds(hwnd)
    p = wintypes.POINT(0, 0)
    _u32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(p))
    c = wintypes.RECT()
    _u32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(c))
    return p.x - l, p.y - t, c.right - c.left, c.bottom - c.top


class WindowCapture:
    """Latest-frame WGC capture of `hwnd`. frame() -> (BGRA ndarray, arrival time) or (None, 0)."""

    def __init__(self, hwnd: int, cursor: bool = False, min_interval_ms: int | None = None):
        from windows_capture import WindowsCapture

        self.hwnd = int(hwnd)
        self._lock = threading.Lock()
        self._new = threading.Condition(self._lock)
        self._frame: np.ndarray | None = None
        self._t = 0.0
        self.count = 0
        self.closed = False
        self.error: str | None = None
        cap = WindowsCapture(cursor_capture=cursor, draw_border=False, window_hwnd=self.hwnd,
                             minimum_update_interval=min_interval_ms)

        @cap.event
        def on_frame_arrived(frame, control):   # noqa: ANN001  (capture thread)
            buf = np.array(frame.frame_buffer, copy=True)
            with self._new:
                self._frame, self._t = buf, time.time()
                self.count += 1
                self._new.notify_all()

        @cap.event
        def on_closed():
            self.closed = True

        self._cap = cap
        self._control = cap.start_free_threaded()

    def frame(self) -> tuple[np.ndarray | None, float]:
        with self._lock:
            return self._frame, self._t

    def wait_first(self, timeout: float = 2.0) -> bool:
        with self._new:
            if self._frame is None:
                self._new.wait(timeout)
            return self._frame is not None

    def close(self) -> None:
        try:
            self._control.stop()
        except Exception:
            pass
