"""
io_win.py -- Screen-capture and mouse-input layer for driving a windowed
Unity (IL2CPP) game such as "Papers, Please" on Windows 11 at high DPI.

All public functions take CLIENT-relative *physical* pixel coordinates
(origin = top-left of the window's client area, units = real device pixels,
i.e. the same units the capture returns). The module makes GetWindowRect,
dxcam/mss capture and SendInput all agree in physical pixels by declaring the
process Per-Monitor-v2 DPI aware at import, *before* any user32/win32 call.

Design notes
------------
* SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE=2) must run before the
  first GUI call. On 10.0.15063+ SetProcessDpiAwarenessContext(-4) (PER_MONITOR
  AWARE V2) is preferred; we try that first and fall back.
* With per-monitor-v2 awareness, win32gui.GetWindowRect / GetClientRect /
  ClientToScreen return true device pixels, dxcam captures device pixels, and
  SendInput's MOUSEEVENTF_ABSOLUTE maps 0..65535 across the *virtual* desktop
  in device pixels. So client(x,y) -> ClientToScreen -> normalized absolute
  SendInput lands exactly at the captured pixel.
* Unity's Input system samples mouse position/button state per frame. A single
  teleport+click can be missed or interpreted as a click without a drag,
  because Unity never sees intermediate positions and the down/up can fall in
  one frame. Drags are therefore interpolated over many SendInput moves with
  small sleeps so Unity observes a moving cursor with the button held.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import time
from typing import Optional, Tuple

import numpy as np

# --------------------------------------------------------------------------
# DPI awareness -- MUST happen before any win32/user32 call in this process.
# --------------------------------------------------------------------------


def _enable_dpi_awareness() -> str:
    """Make the process per-monitor-v2 DPI aware. Returns a short status str."""
    user32 = ctypes.windll.user32
    # Preferred: Per-Monitor-V2 (context handle -4). Win10 1703+.
    try:
        DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
        if user32.SetProcessDpiAwarenessContext(
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        ):
            return "per-monitor-v2 (SetProcessDpiAwarenessContext)"
    except (AttributeError, OSError):
        pass
    # Fallback: shcore PROCESS_PER_MONITOR_DPI_AWARE (= 2). Win8.1+.
    try:
        # 0 = unaware, 1 = system, 2 = per-monitor
        hr = ctypes.windll.shcore.SetProcessDpiAwareness(2)
        if hr == 0:  # S_OK (E_ACCESSDENIED means already set, which is fine)
            return "per-monitor (shcore.SetProcessDpiAwareness)"
        return "per-monitor (already set / shcore hr=%s)" % hr
    except (AttributeError, OSError):
        pass
    # Last resort: legacy system-DPI aware.
    try:
        user32.SetProcessDPIAware()
        return "system-dpi (SetProcessDPIAware)"
    except (AttributeError, OSError):
        return "none"


DPI_STATUS = _enable_dpi_awareness()

# Import win32 modules only AFTER awareness is set.
import win32api  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402


# --------------------------------------------------------------------------
# Window finding
# --------------------------------------------------------------------------


def find_window(title_substring: str) -> int:
    """Return the hwnd of the first visible top-level window whose title
    contains `title_substring` (case-insensitive). Raises if none found."""
    needle = title_substring.lower()
    matches: list[tuple[int, str]] = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if title and needle in title.lower():
            matches.append((hwnd, title))
        return True

    win32gui.EnumWindows(_cb, None)
    if not matches:
        raise LookupError(f"No visible window matching {title_substring!r}")
    return matches[0][0]


def client_rect_physical(hwnd: int) -> Tuple[int, int, int, int]:
    """Return (x, y, w, h) of the window's client area in physical screen
    pixels. (x, y) is the screen position of the client top-left; (w, h) is
    the client size. DPI-aware, so these are true device pixels."""
    # GetClientRect gives (0,0,w,h) in client coords.
    l, t, r, b = win32gui.GetClientRect(hwnd)
    w, h = r - l, b - t
    # ClientToScreen maps the client origin to screen (device) pixels.
    sx, sy = win32gui.ClientToScreen(hwnd, (0, 0))
    return sx, sy, w, h


# --------------------------------------------------------------------------
# Focus / foreground
# --------------------------------------------------------------------------


def focus(hwnd: int) -> None:
    """Bring hwnd to the foreground, using the AttachThreadInput + ALT trick
    to work around Windows' foreground-lock restrictions."""
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(hwnd)
        if win32gui.GetForegroundWindow() == hwnd:
            return
    except Exception:
        pass

    # Foreground lock workaround.
    fg = win32gui.GetForegroundWindow()
    cur_thread = win32api.GetCurrentThreadId()
    fg_thread = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
    tgt_thread = win32process.GetWindowThreadProcessId(hwnd)[0]

    # Send a stray ALT so the OS lets us take foreground.
    for vk in (win32con.VK_MENU,):
        win32api.keybd_event(vk, 0, 0, 0)
        win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)

    attached = []
    try:
        if fg_thread and fg_thread != cur_thread:
            win32process.AttachThreadInput(cur_thread, fg_thread, True)
            attached.append(fg_thread)
        if tgt_thread and tgt_thread != cur_thread and tgt_thread not in attached:
            win32process.AttachThreadInput(cur_thread, tgt_thread, True)
            attached.append(tgt_thread)
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    finally:
        for th in attached:
            try:
                win32process.AttachThreadInput(cur_thread, th, False)
            except Exception:
                pass
    time.sleep(0.05)


# --------------------------------------------------------------------------
# Raw SendInput mouse (absolute positioning in physical pixels)
# --------------------------------------------------------------------------

# Virtual-desktop metrics (device pixels, DPI aware).
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

INPUT_MOUSE = 0

ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


def _virtual_screen() -> Tuple[int, int, int, int]:
    gsm = ctypes.windll.user32.GetSystemMetrics
    return (
        gsm(SM_XVIRTUALSCREEN),
        gsm(SM_YVIRTUALSCREEN),
        gsm(SM_CXVIRTUALSCREEN),
        gsm(SM_CYVIRTUALSCREEN),
    )


def _abs_from_screen(sx: int, sy: int) -> Tuple[int, int]:
    """Map a physical screen pixel to the 0..65535 absolute coord space over
    the whole virtual desktop that SendInput+VIRTUALDESK expects."""
    vx, vy, vw, vh = _virtual_screen()
    # +0.5 to land on the pixel center; -1 because range is inclusive.
    ax = int((sx - vx) * 65535 / (vw - 1) + 0.5)
    ay = int((sy - vy) * 65535 / (vh - 1) + 0.5)
    return max(0, min(65535, ax)), max(0, min(65535, ay))


def _send_mouse(flags: int, ax: int = 0, ay: int = 0) -> None:
    extra = ctypes.c_ulong(0)
    mi = _MOUSEINPUT(ax, ay, 0, flags, 0, ctypes.cast(ctypes.pointer(extra), ULONG_PTR))
    inp = _INPUT(INPUT_MOUSE, _INPUTunion(mi=mi))
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def _move_abs_screen(sx: int, sy: int) -> None:
    ax, ay = _abs_from_screen(sx, sy)
    _send_mouse(
        MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ax, ay
    )


def _client_to_screen(hwnd: int, cx: int, cy: int) -> Tuple[int, int]:
    x, y, _, _ = client_rect_physical(hwnd)
    return x + cx, y + cy


def move(hwnd: int, cx: int, cy: int) -> None:
    """Move the cursor to client-relative physical pixel (cx, cy)."""
    sx, sy = _client_to_screen(hwnd, cx, cy)
    _move_abs_screen(sx, sy)


def click(hwnd: int, cx: int, cy: int, settle: float = 0.03) -> None:
    """Left-click at client-relative physical pixel (cx, cy)."""
    sx, sy = _client_to_screen(hwnd, cx, cy)
    _move_abs_screen(sx, sy)
    time.sleep(settle)
    # Re-assert absolute position on the down event so the click registers
    # exactly where intended regardless of prior cursor state.
    ax, ay = _abs_from_screen(sx, sy)
    _send_mouse(
        MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_LEFTDOWN,
        ax,
        ay,
    )
    time.sleep(settle)
    _send_mouse(MOUSEEVENTF_LEFTUP)
    time.sleep(settle)


def drag(
    hwnd: int,
    ax: int,
    ay: int,
    bx: int,
    by: int,
    duration: float = 0.25,
    steps: int = 16,
) -> None:
    """Press at client (ax, ay), interpolate to (bx, by) over `steps` moves
    across `duration` seconds, then release. Interpolation is required: Unity
    samples the cursor per frame, so a single jump + release is frequently
    seen as a plain click (no drag) because Unity never observes the cursor
    travelling with the button held. ~10-20 steps over ~150-300ms gives Unity
    several frames of motion to register a drag/throw."""
    steps = max(2, steps)
    sx0, sy0 = _client_to_screen(hwnd, ax, ay)
    sx1, sy1 = _client_to_screen(hwnd, bx, by)

    _move_abs_screen(sx0, sy0)
    time.sleep(0.02)
    a0x, a0y = _abs_from_screen(sx0, sy0)
    _send_mouse(
        MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_LEFTDOWN,
        a0x,
        a0y,
    )
    time.sleep(0.04)

    dt = duration / steps
    for i in range(1, steps + 1):
        t = i / steps
        # Smoothstep easing -> more natural motion for Unity's velocity calc.
        te = t * t * (3 - 2 * t)
        ix = int(round(sx0 + (sx1 - sx0) * te))
        iy = int(round(sy0 + (sy1 - sy0) * te))
        _move_abs_screen(ix, iy)
        time.sleep(dt)

    time.sleep(0.03)
    _send_mouse(MOUSEEVENTF_LEFTUP)
    time.sleep(0.03)


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------


class Grabber:
    """Capture the client area of a window as a BGR numpy array.

    Uses dxcam (Desktop Duplication, device pixels, high FPS) and falls back
    to mss if dxcam is unavailable or returns nothing. Call grab() repeatedly;
    the dxcam camera is created once and reused.
    """

    def __init__(self, hwnd: int, prefer: str = "dxcam"):
        self.hwnd = hwnd
        self.prefer = prefer
        self._dxcam = None
        self._camera = None
        self._output_origin = (0, 0)  # top-left of the dxcam output monitor
        self._mss = None
        self._last_frame = None
        if prefer == "dxcam":
            self._init_dxcam()
        if self._camera is None:
            self._init_mss()

    # -- backend init -------------------------------------------------------

    def _init_dxcam(self) -> None:
        try:
            import dxcam

            # Pick the output (monitor) that contains the window.
            x, y, _, _ = client_rect_physical(self.hwnd)
            device_idx, output_idx, origin = self._locate_output(x, y)
            self._dxcam = dxcam
            self._camera = dxcam.create(
                device_idx=device_idx, output_idx=output_idx, output_color="BGR"
            )
            self._output_origin = origin
        except Exception:
            self._dxcam = None
            self._camera = None

    def _locate_output(self, sx: int, sy: int):
        """Find (device_idx, output_idx, (ox, oy)) for the monitor containing
        screen point (sx, sy). Falls back to primary (0,0)."""
        try:
            import dxcam

            info = dxcam.output_info()  # multiline string
            # dxcam enumerates outputs; use win32 monitor geometry to match.
        except Exception:
            pass
        # Use EnumDisplayMonitors to find the monitor rect for the point.
        monitors = win32api.EnumDisplayMonitors()
        for i, (hmon, _hdc, rect) in enumerate(monitors):
            l, t, r, b = rect
            if l <= sx < r and t <= sy < b:
                # dxcam output_idx tends to follow the same enumeration order
                # as EnumDisplayMonitors on a single-GPU machine.
                return 0, i, (l, t)
        return 0, 0, (0, 0)

    def _init_mss(self) -> None:
        import mss

        self._mss = mss.mss()

    # -- capture ------------------------------------------------------------

    def _client_box(self) -> Tuple[int, int, int, int]:
        x, y, w, h = client_rect_physical(self.hwnd)
        return x, y, w, h

    def grab(self) -> np.ndarray:
        """Return a BGR uint8 array (h, w, 3) of the client area."""
        if self._camera is not None:
            img = self._grab_dxcam()
            if img is not None:
                return img
        return self._grab_mss()

    def _grab_dxcam(self) -> Optional[np.ndarray]:
        x, y, w, h = self._client_box()
        ox, oy = self._output_origin
        left = x - ox
        top = y - oy
        region = (left, top, left + w, top + h)
        try:
            frame = self._camera.grab(region=region)
        except Exception:
            return None
        if frame is None:
            # dxcam returns None when the frame is unchanged since the last
            # grab (Desktop Duplication dedup). For a live game the frame
            # almost always changes, so this is rare; when it happens the
            # last captured pixels are still valid, so reuse them. This keeps
            # grab() cheap instead of paying a start/stop resync.
            if self._last_frame is not None:
                return self._last_frame
            # Cold start with no prior frame: do one explicit resync.
            try:
                self._camera.start(region=region, video_mode=False)
                frame = self._camera.get_latest_frame()
                self._camera.stop()
            except Exception:
                frame = None
        if frame is None:
            return None
        self._last_frame = np.ascontiguousarray(frame)
        return self._last_frame

    def _grab_mss(self) -> np.ndarray:
        x, y, w, h = self._client_box()
        if self._mss is None:
            self._init_mss()
        shot = self._mss.grab({"left": x, "top": y, "width": w, "height": h})
        arr = np.asarray(shot)  # BGRA
        return np.ascontiguousarray(arr[:, :, :3])  # drop alpha -> BGR

    def close(self) -> None:
        if self._camera is not None:
            try:
                self._camera.release()
            except Exception:
                pass
            self._camera = None
        if self._mss is not None:
            try:
                self._mss.close()
            except Exception:
                pass
            self._mss = None


# --------------------------------------------------------------------------
# PrintWindow fallback (for occluded / background windows)
# --------------------------------------------------------------------------


def print_window(hwnd: int) -> np.ndarray:
    """Capture a window's client area via PrintWindow (works even when the
    window is occluded). Returns BGR uint8 (h, w, 3). Uses
    PW_RENDERFULLCONTENT (=3) which captures many hardware/DWM surfaces that
    the older PrintWindow(flag=1) misses. Note: some GPU-composited surfaces
    (incl. some Unity swapchains) can still come back black -- prefer Grabber
    for the live game and keep this as a diagnostic fallback."""
    import win32ui
    from ctypes import windll

    l, t, r, b = win32gui.GetClientRect(hwnd)
    w, h = r - l, b - t
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bmp)
    windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 3)  # PW_RENDERFULLCONTENT
    info = bmp.GetInfo()
    bits = bmp.GetBitmapBits(True)
    arr = np.frombuffer(bits, dtype=np.uint8).reshape(
        (info["bmHeight"], info["bmWidth"], 4)
    )
    out = np.ascontiguousarray(arr[:, :, :3])  # BGRA -> BGR
    win32gui.DeleteObject(bmp.GetHandle())
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)
    return out


if __name__ == "__main__":
    print("DPI status:", DPI_STATUS)
    print("Virtual screen:", _virtual_screen())
