"""Print the geometry the Cap zoom plan needs: game client rect, viewer window rect, and the display they are on.

  python tools/window_rects.py                     # print JSON
  python tools/window_rects.py --out runs/rects.json
  python tools/window_rects.py --viewer-title "TOD viewer"

All values are PHYSICAL (device) pixels -- the same space Cap records a display in. io_win is imported first so the
process is per-monitor DPI aware (otherwise Windows reports scaled/virtualised coordinates). Read-only: no focus
change, no input. Keys:

  display          {name, rect:[x,y,w,h], primary}   the monitor that holds the game window (what Cap should record)
  game_client      [x,y,w,h] screen coords of the game's client area (UnityWndClass "PapersPlease")
  game_in_display  [x,y,w,h] game_client relative to the display origin  <- tools/cap_zoom_plan.py --rects reads this
  viewer_window    [x,y,w,h] visible frame of the viewer window (DWM extended frame bounds), or null
  tally_window / tally_in_display  the tally/scoreboard window (only while it is up; run with it showing)
  viewer_in_display, game_window (frame incl. title bar), monitors (all), dpi_awareness
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from tod_papers import io_win  # noqa: E402  (sets per-monitor DPI awareness before win32 calls)
import win32api  # noqa: E402
import win32gui  # noqa: E402

GAME_CLASS, GAME_TITLE = "UnityWndClass", "PapersPlease"


def _frame_bounds(hwnd: int):
    """Visible window rect (excludes the invisible resize border GetWindowRect includes on Win10/11)."""
    r = wintypes.RECT()
    try:
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 9, ctypes.byref(r), ctypes.sizeof(r)) == 0:
            return [r.left, r.top, r.right - r.left, r.bottom - r.top]
    except OSError:
        pass
    l, t, rr, b = win32gui.GetWindowRect(hwnd)
    return [l, t, rr - l, b - t]


def _find(pred):
    out = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and pred(h):
            out.append(h)
        return True
    win32gui.EnumWindows(cb, None)
    return out[0] if out else None


def _monitor(hwnd_or_none):
    mons = []
    for hmon, _hdc, (l, t, r, b) in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(hmon)
        mons.append({"name": info.get("Device"), "rect": [l, t, r - l, b - t], "primary": bool(info.get("Flags", 0) & 1),
                     "_h": int(hmon)})
    cur = None
    if hwnd_or_none:
        h = int(win32api.MonitorFromWindow(hwnd_or_none, 2))   # MONITOR_DEFAULTTONEAREST
        cur = next((m for m in mons if m["_h"] == h), None)
    cur = cur or next((m for m in mons if m["primary"]), mons[0])
    for m in mons:
        m.pop("_h")
    return cur, mons


def rel(rect, disp):
    return None if rect is None else [rect[0] - disp[0], rect[1] - disp[1], rect[2], rect[3]]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--viewer-title", default="TOD viewer", help="substring of the viewer window title")
    ap.add_argument("--tally-title", default="TOD tally",
                    help="substring of the tally/scoreboard window title (tools/tally.py; verify once it exists)")
    ap.add_argument("--out", help="also write the JSON here (input for cap_zoom_plan.py --rects)")
    a = ap.parse_args(argv)

    game = _find(lambda h: win32gui.GetClassName(h) == GAME_CLASS and GAME_TITLE.lower() in win32gui.GetWindowText(h).lower())
    needle = a.viewer_title.lower()
    viewer = _find(lambda h: needle in win32gui.GetWindowText(h).lower())
    tneedle = a.tally_title.lower()
    tally = _find(lambda h: tneedle in win32gui.GetWindowText(h).lower())
    disp, mons = _monitor(game or viewer)
    game_client = list(io_win.client_rect_physical(game)) if game else None
    viewer_rect = _frame_bounds(viewer) if viewer else None
    res = {
        "display": disp,
        "game_client": game_client,
        "game_in_display": rel(game_client, disp["rect"]),
        "game_window": _frame_bounds(game) if game else None,
        "viewer_window": viewer_rect,
        "viewer_in_display": rel(viewer_rect, disp["rect"]),
        "tally_window": _frame_bounds(tally) if tally else None,
        "tally_in_display": rel(_frame_bounds(tally), disp["rect"]) if tally else None,
        "monitors": mons,
        "dpi_awareness": io_win.DPI_STATUS,
    }
    notes = []
    if not game:
        notes.append("game window not found (UnityWndClass / PapersPlease)")
    elif win32gui.IsIconic(game):
        notes.append("game window is MINIMIZED (rect -32000): restore it, then re-run")
    if not viewer:
        notes.append(f"viewer window not found (title contains {a.viewer_title!r})")
    if game and viewer:
        dx, dy, dw, dh = disp["rect"]
        for nm, r in (("game", game_client), ("viewer", viewer_rect)):
            if r[0] < dx or r[1] < dy or r[0] + r[2] > dx + dw or r[1] + r[3] > dy + dh:
                notes.append(f"{nm} window is not fully inside display {disp['name']} -- it will be cut off in Cap")
        g, v = game_client, viewer_rect
        if not (v[0] >= g[0] + g[2] or v[0] + v[2] <= g[0] or v[1] >= g[1] + g[3] or v[1] + v[3] <= g[1]):
            notes.append("viewer overlaps the game window")
    res["notes"] = notes
    txt = json.dumps(res, indent=2)
    print(txt)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(txt + "\n")
    return 0 if game and not win32gui.IsIconic(game) else 1


if __name__ == "__main__":
    sys.exit(main())
