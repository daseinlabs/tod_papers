r"""TOD stage: ONE fixed-size window that composes everything the demo video shows. Cap records this window.

    .venv-loop\Scripts\python.exe tools\stage.py --follow                       live: newest run under runs/
    .venv-loop\Scripts\python.exe tools\stage.py --replay runs/<ts> [--fps 2] [--start N]
    .venv-loop\Scripts\python.exe tools\stage.py --print-rects [--size WxH]     print the tile rects and exit

Tiles (window-client px; default size 3456x1944 = the display's top 16:9 band, see below):
  A  IN     the game, LIVE: Windows.Graphics.Capture of the game hwnd (src/tod_papers/wgc.py), ~30 fps, cursor
            included; occlusion-independent, so the game may sit anywhere (under this window, another monitor).
            Minimized / gone: the last frame stays. The game window is re-found every 2 s (restarts are fine).
  B  TOD    the sidebar, rendered in-process by src/tod_papers/viewer_video.py (VideoPanel in its three-tile
            layout): one held still per tick with the renderer's own transitions, driven by the run log.
  C  TALLY  src/tod_papers/tally.py's renderer, over the sidebar side (16:9, right half of the window), shown for
            --secs at each day end and held after the final day / run end -- the triggers of tools/tally.py
            (TL.day_end_events, run_summary). Every showing is logged to <run>/tally_schedule.json (tools/tally.py
            Schedule format) for tools/cap_zoom_plan.py. `tools/tally.py --show-now [S]` pops it here too.
  Background black; nothing of the desktop is ever drawn.

Why on top + click-through: Cap 0.6 on Windows records a Window target as a DISPLAY capture cropped to the window
rect (crates/recording/src/capture_pipeline.rs; only its screenshots use per-window capture), so whatever is on
screen over that rect is recorded. The stage therefore sits on top (normal topmost band, set once), never takes
focus, and passes all mouse input through (WS_EX_LAYERED|WS_EX_TRANSPARENT|WS_EX_NOACTIVATE) -- TOD's clicks
reach the game underneath. The loop must then grab with TOD_GRAB=wgc (src/tod_papers/io_win.py), since a screen
grab would see the stage. It must also lie fully on one display: hence the default 3456x1944 at (0, 0), which
cap_record exports to 3840x2160. --no-on-top / --no-click-through / --size / --pos change that.

Rects go to stdout, runs/stage_rects.json and runs/<ts>/stage_rects.json (followed run, or the replay out dir):
  {"stage": true, "size": [W, H], "IN": A, "TWOUP": [0, 0, W, H], "TOD": [t1, t2, t3], "TALLY": C, ...}
plus the keys cap_zoom_plan --rects already reads (game_in_display, display.rect, tiles, tally_in_display).
Read-only on the run dir apart from tally_schedule.json / stage_rects.json; never talks to the loop or the game.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import importlib.util
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

from tod_papers import io_win  # noqa: E402,F401  (per-monitor-v2 DPI awareness before any window call)
from tod_papers import tally as TL  # noqa: E402
from tod_papers import viewer as V  # noqa: E402
from tod_papers import viewer_video as VV  # noqa: E402
from tod_papers import wgc  # noqa: E402

import cv2  # noqa: E402
import numpy as np  # noqa: E402

RUNS = os.path.join(ROOT, "runs")
TITLE = "TOD stage"
GAME_W, GAME_H = VV.GAME_W, VV.GAME_H

_spec = importlib.util.spec_from_file_location("tally_tool", os.path.join(HERE, "tally.py"))
TT = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(TT)   # Schedule + SIGNAL, shared with tools/tally.py


# ------------------------------------------------------------------------------------------------------------ layout
def layout(W: int, H: int) -> dict:
    side_w = round(H / 3 * VV.TILE_W / VV.TILE_H)          # 1176 at H=1944: VideoPanel's three-tile layout
    vp = VV.VideoPanel(side_w, H)
    if not vp.tiles:
        sys.exit(f"--size {W}x{H}: sidebar {side_w}x{H} is not VideoPanel's three-tile layout")
    gw = W - side_w
    s = min(gw / GAME_W, H / GAME_H)
    aw, ah = round(GAME_W * s), round(GAME_H * s)
    A = [(gw - aw) // 2, (H - ah) // 2, aw, ah]
    tods = [list(r) for r in VV.tile_rects(gw, 0, side_w, vp.tile_h)]
    tw = W // 2
    th = round(tw * 9 / 16)
    C = [W - tw, (H - th) // 2, tw, th]
    return {"stage": True, "title": TITLE, "size": [W, H], "IN": A, "TWOUP": [0, 0, W, H], "TOD": tods,
            "TALLY": C, "SIDEBAR": [gw, 0, side_w, H], "game_scale": round(s, 4),
            # the keys tools/cap_zoom_plan.py --rects reads (frame = this window, no crop)
            "display": {"rect": [0, 0, W, H]}, "game_in_display": A, "tiles": tods, "tally_in_display": C}


def write_rects(R: dict, run_dir: str | None) -> None:
    for d in filter(None, (RUNS, run_dir)):
        try:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "stage_rects.json"), "w", encoding="utf-8") as fh:
                json.dump(R, fh, indent=1)
        except OSError as e:
            print(f"[stage] cannot write rects to {d}: {e}")


# ------------------------------------------------------------------------------------------------------------- win32
u32 = ctypes.WinDLL("user32", use_last_error=True)
g32 = ctypes.WinDLL("gdi32")
k32 = ctypes.WinDLL("kernel32")
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
u32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.DefWindowProcW.restype = LRESULT
u32.CreateWindowExW.restype = wt.HWND
u32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
u32.GetDC.restype = wt.HDC
u32.GetDC.argtypes = [wt.HWND]
u32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
u32.BeginPaint.restype = wt.HDC
g32.StretchDIBits.argtypes = [wt.HDC] + [ctypes.c_int] * 8 + [ctypes.c_void_p, ctypes.c_void_p, wt.UINT, wt.DWORD]
g32.ExcludeClipRect.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
g32.SelectClipRgn.argtypes = [wt.HDC, wt.HRGN]
g32.SetStretchBltMode.argtypes = [wt.HDC, ctypes.c_int]
g32.SetBrushOrgEx.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
u32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
u32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
u32.DestroyWindow.argtypes = [wt.HWND]
g32.CreateSolidBrush.restype = wt.HBRUSH
g32.GetStockObject.restype = wt.HGDIOBJ
u32.FillRect.argtypes = [wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH]
u32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
u32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
u32.LoadCursorW.restype = wt.HANDLE
u32.LoadCursorW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]

WS_POPUP, WS_VISIBLE = 0x80000000, 0x10000000
WS_EX_TOPMOST, WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_NOACTIVATE = 0x8, 0x80000, 0x20, 0x08000000
WS_EX_APPWINDOW = 0x40000
WM_DESTROY, WM_PAINT, WM_CLOSE, WM_ERASEBKGND, WM_KEYDOWN = 0x0002, 0x000F, 0x0010, 0x0014, 0x0100
WM_MOUSEACTIVATE, MA_NOACTIVATE = 0x0021, 3
PM_REMOVE, SRCCOPY, DIB_RGB_COLORS, LWA_ALPHA = 1, 0x00CC0020, 0, 2
HALFTONE, COLORONCOLOR = 4, 3
HWND_TOPMOST = wt.HWND(-1)
SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x10, 0x40


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wt.HDC), ("fErase", wt.BOOL), ("rcPaint", wt.RECT), ("fRestore", wt.BOOL),
                ("fIncUpdate", wt.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


u32.BeginPaint.argtypes = [wt.HWND, ctypes.POINTER(PAINTSTRUCT)]
u32.EndPaint.argtypes = [wt.HWND, ctypes.POINTER(PAINTSTRUCT)]


def blit(hdc, img: np.ndarray, rect, stretch: bool = True) -> None:
    """BGRA uint8 (h, w, 4) C-contiguous -> rect (x, y, w, h) of the window DC."""
    h, w = img.shape[:2]
    bi = BITMAPINFOHEADER(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    x, y, dw, dh = rect
    g32.SetStretchBltMode(hdc, HALFTONE if (dw, dh) != (w, h) else COLORONCOLOR)
    g32.SetBrushOrgEx(hdc, 0, 0, None)
    g32.StretchDIBits(hdc, x, y, dw, dh, 0, 0, w, h, img.ctypes.data, ctypes.byref(bi), DIB_RGB_COLORS, SRCCOPY)


def pil_bgra(im) -> np.ndarray:
    a = np.asarray(im.convert("RGB"))
    return np.ascontiguousarray(cv2.cvtColor(a, cv2.COLOR_RGB2BGRA))


# ------------------------------------------------------------------------------------------------------- content
class Content(threading.Thread):
    """Sidebar + tally state machine on its own thread; renders into BGRA buffers the window thread blits."""

    def __init__(self, a, R: dict):
        super().__init__(daemon=True)
        self.a, self.R = a, R
        sx, _, sw, sh = R["SIDEBAR"]
        self.vp = VV.VideoPanel(sw, sh)
        self.ren = TL.TallyRenderer()
        self.lock = threading.Lock()
        self.side_img = None      # BGRA sidebar frame
        self.side_ver = 0
        self.tally_img = None     # BGRA tally (current showing)
        self.tally_on = False
        self.tally_ver = 0
        self.stop = False
        self.reader = None
        self.run_dir = None
        self.scene = self.prev = None
        self.t0 = 0.0
        self.cur = None
        self.out_dir = None
        self.shown: list[dict] = []
        # tally
        self.rt = None
        self.fired: set = set()
        self.final = False
        self.until = None
        self.cur_entry = None
        self.cur_kind = None
        self.sch = None

    # -- run attach ----------------------------------------------------------------------------------------------
    def attach(self, run_dir: str, initial: bool) -> None:
        self.run_dir = run_dir
        self.reader = V.RunReader(run_dir)
        self.scene = self.prev = None
        self.cur = None
        self.rt = TL.RunTicks(run_dir)
        self.rt.poll()
        self.fired, self.final, self.until, self.cur_entry = set(), False, None, None
        self._set_tally(None)
        if self.a.replay:
            ts = time.strftime("%Y%m%d_%H%M%S")
            self.out_dir = os.path.join(RUNS, f"stage_replay_{ts}")
            os.makedirs(self.out_dir, exist_ok=True)
            self.sch = TT.Schedule(self.out_dir, fresh=True)   # never rewrite the replayed run's own schedule
        else:
            self.out_dir = run_dir
            self.sch = TT.Schedule(run_dir)
            if initial:   # events already in the log when we attached are history, not a cue (as tools/tally.py)
                self.fired = {(e["kind"], e["day"]) for e in TL.day_end_events(self.rt.ticks, self.a.final_day)}
                if TL.run_summary(run_dir, self.rt.ticks):
                    self.fired.add(("run_end", None))
        R = dict(self.R, run=run_dir, replay=bool(self.a.replay))
        write_rects(R, self.out_dir)
        print(f"[stage] {'replaying' if self.a.replay else 'following'} {run_dir} ({len(self.rt.ticks)} ticks)"
              f"; rects + tally schedule -> {self.out_dir}", flush=True)

    # -- sidebar -------------------------------------------------------------------------------------------------
    def show_tick(self, t: int) -> bool:
        sc = VV.build_scene(self.reader, t, self.vp.som_size)
        if sc is None:
            return False
        self.prev = self.scene if self.scene is not None and abs(t - self.scene.tick) <= 3 else None
        self.scene, self.t0, self.cur = sc, time.time(), t
        self.shown.append({"tick": t, "time": round(self.t0, 3)})
        if self.a.replay and self.out_dir:
            with open(os.path.join(self.out_dir, "replay_ticks.json"), "w", encoding="utf-8") as fh:
                json.dump({"run": self.run_dir, "shown": self.shown}, fh)
        return True

    def ready(self, t: int) -> bool:
        rd = self.run_dir
        if os.path.exists(os.path.join(rd, f"tick_{t:04d}.png")):
            return True
        try:
            return time.time() - os.path.getmtime(os.path.join(rd, f"tick_{t:04d}.json")) > 3
        except OSError:
            return False

    def render_side(self) -> bool:
        """Draw the current animation frame; False once the transition is over (frame is held)."""
        if self.scene is None:
            return False
        t = time.time() - self.t0
        img = pil_bgra(self.vp.draw(self.scene, self.prev, t))
        with self.lock:
            self.side_img, self.side_ver = img, self.side_ver + 1
        return t < VV.ANIM_END + 0.3

    # -- tally ---------------------------------------------------------------------------------------------------
    def _set_tally(self, img) -> None:
        with self.lock:
            self.tally_img, self.tally_on = img, img is not None
            self.tally_ver += 1

    def tally_image(self, kind: str, upto: int | None):
        ticks = self.rt.ticks
        final = kind in ("final", "run_end")
        if self.a.replay:
            T = TL.tally_for(self.run_dir, ticks, None if final else upto, final_day=self.a.final_day)
        else:
            T = TL.tally_for(self.run_dir, ticks, final_day=self.a.final_day)
        if final:
            T["kind"] = "final"
        _, _, w, h = self.R["TALLY"]
        return pil_bgra(self.ren.render(T, (w, h)))

    def show_tally(self, kind: str, day, tick, why: str, secs: float | None) -> None:
        now = time.time()
        if self.cur_entry:
            self.sch.close(self.cur_entry, now)
        self._set_tally(self.tally_image(kind, tick))
        self.cur_entry = self.sch.add(now, None, f"{kind} day {day} tick {tick}: {why}")
        self.cur_kind = kind
        self.until = (now + secs) if secs else None
        print(f"[stage] tally {kind} day={day} tick={tick} ({why})" + (f" for {secs:.0f}s" if secs else
                                                                         " (stays up)"), flush=True)

    def hide_tally(self) -> None:
        if self.cur_entry:
            self.sch.close(self.cur_entry, time.time())
        self.cur_entry, self.until, self.cur_kind = None, None, None
        self._set_tally(None)

    def tally_triggers(self) -> None:
        ticks = self.rt.ticks
        if self.a.replay:   # only what the replay has reached
            ticks = [r for r in ticks if self.cur is not None and r["tick"] <= self.cur]
        for e in TL.day_end_events(ticks, self.a.final_day):
            k = (e["kind"], e["day"])
            if k not in self.fired:
                self.fired.add(k)
                self.final = self.final or e["kind"] == "final"
                self.show_tally(e["kind"], e["day"], e["tick"], e["why"], None if e["kind"] == "final" else self.a.secs)
        at_end = (not self.a.replay) or (self.cur is not None and ticks and self.cur == self.rt.ticks[-1]["tick"])
        if ("run_end", None) not in self.fired and at_end and TL.run_summary(self.run_dir, self.rt.ticks):
            self.fired.add(("run_end", None))
            self.final = True
            last = ticks[-1] if ticks else {}
            if not (self.cur_entry and self.cur_kind == "final"):
                self.show_tally("run_end", (last.get("gt") or {}).get("day"), last.get("tick"), "run finished", None)
        if os.path.exists(TT.SIGNAL):   # tools/tally.py --show-now [SECS]
            try:
                with open(TT.SIGNAL, encoding="utf-8") as fh:
                    s = fh.read().strip()
                os.remove(TT.SIGNAL)
            except OSError:
                s = ""
            secs = float(s) if s.replace(".", "", 1).isdigit() else self.a.secs
            if not (self.cur_entry and self.cur_kind in ("final", "run_end")):
                last = ticks[-1] if ticks else {}
                self.show_tally("manual", (last.get("gt") or {}).get("day"), last.get("tick"), "--show-now", secs)
        if self.cur_entry and self.until and time.time() >= self.until:
            self.hide_tally()

    # -- main ----------------------------------------------------------------------------------------------------
    def run(self) -> None:
        a = self.a
        if a.replay:
            self.attach(os.path.abspath(a.replay), initial=False)
        next_t, animating, last_poll = time.time(), False, 0.0
        while not self.stop:
            try:
                now = time.time()
                if now - last_poll >= (0.05 if a.replay else a.poll):
                    last_poll = now
                    if not a.replay:
                        nr = V.newest_run(RUNS)
                        if nr and (self.run_dir is None or os.path.normcase(nr) != os.path.normcase(self.run_dir)):
                            self.attach(nr, initial=self.run_dir is None and not a.fire_existing)
                    if self.rt is not None:
                        self.rt.poll()
                        ts = [t for t in V.list_ticks(self.run_dir) if a.start is None or t >= a.start]
                        if a.replay:
                            if now >= next_t:
                                nxt = [t for t in ts if self.cur is None or t > self.cur]
                                if nxt and self.show_tick(nxt[0]):
                                    animating = True
                                next_t = now + 1.0 / max(a.fps, 0.01)
                        elif ts:
                            for t in reversed(ts[-3:]):
                                if t != self.cur and self.ready(t):
                                    if (self.cur is None or t > self.cur) and self.show_tick(t):
                                        animating = True
                                    break
                        self.tally_triggers()
                if animating:
                    animating = self.render_side()
                else:
                    time.sleep(0.02)
            except Exception as e:   # a half-written file etc.: never die mid-recording
                print(f"[stage] content error: {e!r}", flush=True)
                time.sleep(0.2)


# ------------------------------------------------------------------------------------------------------------ mirror
class Mirror:
    """WGC capture of the game window; re-finds the window every 2 s; keeps the last frame."""

    def __init__(self, title_hint: str | None):
        self.cap = None
        self.hwnd = None
        self.last = None          # BGRA client crop (contiguous)
        self.ver = 0
        self._seen = -1
        self._next_find = 0.0
        self.err = None

    def find(self) -> int | None:
        import reset_game as rg
        try:
            return rg.unity_window_of({p.pid for p in rg.game_procs()})
        except Exception:
            return None

    def poll(self) -> None:
        now = time.time()
        if now >= self._next_find:
            self._next_find = now + 2.0
            alive = self.hwnd and u32.IsWindow(wt.HWND(self.hwnd)) and self.cap and not self.cap.closed
            if not alive:
                h = self.find()
                if h and h != self.hwnd:
                    if self.cap:
                        self.cap.close()
                    try:
                        self.cap, self.hwnd, self._seen = wgc.WindowCapture(h, cursor=True), h, -1
                        print(f"[stage] mirroring game hwnd {h} (WGC)", flush=True)
                    except Exception as e:
                        self.cap, self.hwnd = None, None
                        if repr(e) != self.err:
                            print(f"[stage] WGC capture failed: {e!r}", flush=True)
                        self.err = repr(e)
        if not self.cap:
            return
        f, _ = self.cap.frame()
        if f is None or self.cap.count == self._seen:
            return
        self._seen = self.cap.count
        if u32.IsIconic(wt.HWND(self.hwnd)):
            return
        x, y, w, h = wgc.client_in_frame(self.hwnd)
        x, y = max(0, x), max(0, y)
        w, h = min(w, f.shape[1] - x), min(h, f.shape[0] - y)
        if w < 64 or h < 64:
            return
        roi = f[y:y + h, x:x + w]
        self.last = roi if roi.flags["C_CONTIGUOUS"] else np.ascontiguousarray(roi)
        self.ver += 1


def fit(rect, w: int, h: int):
    x, y, rw, rh = rect
    s = min(rw / w, rh / h)
    dw, dh = round(w * s), round(h * s)
    return x + (rw - dw) // 2, y + (rh - dh) // 2, dw, dh


# -------------------------------------------------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="TOD stage window (one window for Cap to record)")
    m = ap.add_mutually_exclusive_group()
    m.add_argument("--follow", action="store_true", help="newest run under runs/ (default)")
    m.add_argument("--replay", metavar="RUN_DIR", help="replay a finished run's sidebar + tally")
    ap.add_argument("--fps", type=float, default=2.0, help="--replay: ticks per second")
    ap.add_argument("--start", type=int, default=None, help="first tick to show")
    ap.add_argument("--size", default="3456x1944", help="client size WxH (fixed; default 3456x1944)")
    ap.add_argument("--pos", default="0,0", help="screen position X,Y of the window (physical px)")
    ap.add_argument("--mirror-fps", type=float, default=30.0)
    ap.add_argument("--secs", type=float, default=12.0, help="seconds a day-end tally stays up (final stays up)")
    ap.add_argument("--final-day", type=int, default=3)
    ap.add_argument("--poll", type=float, default=0.25)
    ap.add_argument("--fire-existing", action="store_true", help="fire tally events already in the log at start")
    ap.add_argument("--no-on-top", dest="on_top", action="store_false")
    ap.add_argument("--no-click-through", dest="click_through", action="store_false")
    ap.add_argument("--print-rects", action="store_true", help="print the tile rects (JSON) and exit")
    a = ap.parse_args(argv)
    W, H = (int(v) for v in a.size.lower().split("x"))
    px, py = (int(v) for v in a.pos.split(","))
    R = layout(W, H)
    R["screen_origin"] = [px, py]
    if a.print_rects:
        print(json.dumps({k: R[k] for k in ("size", "IN", "TWOUP", "TOD", "TALLY", "SIDEBAR", "game_scale")}))
        return 0
    print("[stage] rects " + json.dumps({k: R[k] for k in ("size", "IN", "TWOUP", "TOD", "TALLY")}), flush=True)
    write_rects(R, None)

    # ---- window
    hinst = k32.GetModuleHandleW(None)
    state = {"full": True}
    content = Content(a, R)
    mirror = Mirror(None)

    def paint_all(hdc):
        r = wt.RECT(0, 0, W, H)
        u32.FillRect(hdc, ctypes.byref(r), g32.GetStockObject(4))   # BLACK_BRUSH
        with content.lock:
            side, tal, on = content.side_img, content.tally_img, content.tally_on
        if mirror.last is not None:
            blit(hdc, mirror.last, fit(R["IN"], mirror.last.shape[1], mirror.last.shape[0]))
        if side is not None:
            blit(hdc, side, R["SIDEBAR"])
        if on and tal is not None:
            blit(hdc, tal, R["TALLY"])

    @WNDPROC
    def wndproc(hwnd, msg, wp, lp):
        if msg == WM_PAINT:
            ps = PAINTSTRUCT()
            hdc = u32.BeginPaint(hwnd, ctypes.byref(ps))
            paint_all(hdc)
            u32.EndPaint(hwnd, ctypes.byref(ps))
            return 0
        if msg == WM_ERASEBKGND:
            return 1
        if msg == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE
        if msg == WM_KEYDOWN and wp in (0x1B, 0x51):   # Esc / Q (only if it ever has focus)
            u32.DestroyWindow(hwnd)
            return 0
        if msg == WM_CLOSE:
            u32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            u32.PostQuitMessage(0)
            return 0
        return u32.DefWindowProcW(hwnd, msg, wp, lp)

    wc = WNDCLASSW(0, wndproc, 0, 0, hinst, None, u32.LoadCursorW(None, ctypes.c_void_p(32512)),
                   g32.GetStockObject(4), None, "TODStageWindow")
    if not u32.RegisterClassW(ctypes.byref(wc)):
        sys.exit(f"RegisterClassW failed: {ctypes.get_last_error()}")
    ex = WS_EX_APPWINDOW | WS_EX_NOACTIVATE
    if a.on_top:
        ex |= WS_EX_TOPMOST
    if a.click_through:
        ex |= WS_EX_LAYERED | WS_EX_TRANSPARENT
    hwnd = u32.CreateWindowExW(ex, "TODStageWindow", TITLE, WS_POPUP, px, py, W, H, None, None, hinst, None)
    if not hwnd:
        sys.exit(f"CreateWindowExW failed: {ctypes.get_last_error()}")
    if a.click_through:
        u32.SetLayeredWindowAttributes(wt.HWND(hwnd), 0, 255, LWA_ALPHA)   # opaque; layered only for hit-testing
    u32.SetWindowPos(hwnd, HWND_TOPMOST if a.on_top else None, px, py, W, H, SWP_NOACTIVATE | SWP_SHOWWINDOW)
    print(f"[stage] window \"{TITLE}\" hwnd {hwnd} {W}x{H} at ({px},{py}); on_top={a.on_top} "
          f"click_through={a.click_through}", flush=True)
    content.start()

    msg = wt.MSG()
    period = 1.0 / max(1.0, a.mirror_fps)
    seen = {"game": -1, "side": -1, "tally": -1, "on": False}
    nxt = time.perf_counter()
    try:
        while True:
            while u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == 0x0012:   # WM_QUIT
                    return 0
                u32.TranslateMessage(ctypes.byref(msg))
                u32.DispatchMessageW(ctypes.byref(msg))
            mirror.poll()
            with content.lock:
                side, sv = content.side_img, content.side_ver
                tal, tv, on = content.tally_img, content.tally_ver, content.tally_on
            hdc = u32.GetDC(hwnd)
            try:
                if seen["on"] and not on:          # tally went away: repaint everything under it
                    paint_all(hdc)
                    seen.update(game=mirror.ver, side=sv, tally=tv, on=on)
                else:
                    if on:   # the tally covers part of A and B: never draw over it
                        tx, ty, tw, th = R["TALLY"]
                        g32.ExcludeClipRect(hdc, tx, ty, tx + tw, ty + th)
                    if mirror.ver != seen["game"] and mirror.last is not None:
                        blit(hdc, mirror.last, fit(R["IN"], mirror.last.shape[1], mirror.last.shape[0]))
                        seen["game"] = mirror.ver
                    if sv != seen["side"] and side is not None:
                        blit(hdc, side, R["SIDEBAR"])
                        seen["side"] = sv
                    g32.SelectClipRgn(hdc, None)
                    if on and (tv != seen["tally"] or not seen["on"]) and tal is not None:
                        blit(hdc, tal, R["TALLY"])
                    seen.update(tally=tv, on=on)
            finally:
                u32.ReleaseDC(hwnd, hdc)
            nxt += period
            d = nxt - time.perf_counter()
            if d > 0:
                time.sleep(d)
            else:
                nxt = time.perf_counter()
    except KeyboardInterrupt:
        return 0
    finally:
        content.stop = True
        if mirror.cap:
            mirror.cap.close()


if __name__ == "__main__":
    sys.exit(main())
