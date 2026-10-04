"""Behind-the-scenes window for screen recording (docs/viewer.md).

  python tools/viewer.py --run runs/<ts>            live: show each new tick as the loop writes it
  python tools/viewer.py --follow                   live: newest run dir, switch when a new run starts
  python tools/viewer.py --run runs/<ts> --replay --fps 2 [--start 130 --end 140]
  python tools/viewer.py --run runs/<ts> --export out_dir [--start N --end M]   (PNGs, no window)

Read-only: polls the run dir, never touches the loop or the game. Keys: space pause, Left/Right step,
Home/End first/last, q/Esc quit.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from tod_papers import viewer as V  # noqa: E402


def game_rect():
    """Screen rect (l, t, r, b) of the game window, or None. Read-only (no focus, no input)."""
    try:
        import win32gui
    except ImportError:
        return None
    found = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and "papers" in win32gui.GetWindowText(h).lower() \
                and "viewer" not in win32gui.GetWindowText(h).lower():
            found.append(h)
        return True
    win32gui.EnumWindows(cb, None)
    return win32gui.GetWindowRect(found[0]) if found else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="run dir (runs/<ts>)")
    ap.add_argument("--follow", action="store_true", help="use the newest run dir under runs/, switch to newer ones")
    ap.add_argument("--replay", action="store_true", help="play a finished run from --start at --fps")
    ap.add_argument("--fps", type=float, default=1.0, help="replay ticks per second")
    ap.add_argument("--start", type=int, default=None)
    ap.add_argument("--end", type=int, default=None)
    ap.add_argument("--export", help="write panel PNGs to this dir and exit (no window)")
    ap.add_argument("--width", type=int, default=1160)
    ap.add_argument("--height", type=int, default=1980)
    ap.add_argument("--x", type=int, default=None, help="window left (physical px); default: right of the game")
    ap.add_argument("--y", type=int, default=None)
    ap.add_argument("--poll", type=float, default=0.25, help="live poll interval, s")
    a = ap.parse_args()

    runs_root = os.path.join(ROOT, "runs")
    run_dir = a.run if a.run else V.newest_run(runs_root)
    if not run_dir or not os.path.isdir(run_dir):
        sys.exit(f"no run dir ({run_dir})")
    reader = V.RunReader(run_dir)
    panel = V.Panel(a.width, a.height)

    if a.export:
        os.makedirs(a.export, exist_ok=True)
        for t in V.list_ticks(run_dir):
            if (a.start is not None and t < a.start) or (a.end is not None and t > a.end):
                continue
            im = V.render_tick(reader, panel, t, "export")
            if im is not None:
                im.save(os.path.join(a.export, f"view_{t:04d}.png"))
        print(f"exported to {a.export}")
        return

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # physical pixels, crisp text at 225 % scaling
    except Exception:
        pass
    import tkinter as tk
    from PIL import ImageTk

    root = tk.Tk()
    root.title("TOD viewer")
    root.configure(bg="#121418")
    x, y = a.x, a.y
    if x is None:
        r = game_rect()
        x, y = (r[2] + 4, r[1] if y is None else y) if r else (2290, 0 if y is None else y)
    root.geometry(f"{a.width}x{a.height}+{x}+{y or 0}")
    lab = tk.Label(root, bg="#121418", bd=0)
    lab.pack(fill="both", expand=True)

    st = {"reader": reader, "cur": None, "paused": False, "photo": None, "next_t": time.time()}
    mode = "replay" if a.replay else ("follow" if a.follow else "live")

    def show(t):
        im = V.render_tick(st["reader"], panel, t, mode)
        if im is None:
            return False
        st["photo"] = ImageTk.PhotoImage(im)
        lab.configure(image=st["photo"])
        st["cur"] = t
        root.title(f"TOD viewer - {os.path.basename(st['reader'].run_dir)} t{t}")
        return True

    def ticks():
        ts = V.list_ticks(st["reader"].run_dir)
        return [t for t in ts if (a.start is None or t >= a.start) and (a.end is None or t <= a.end)]

    def ready(t):
        """Live: the tick's SoM png is on disk (the loop writes it on a pool thread), or the json is 3 s old."""
        rd = st["reader"].run_dir
        if os.path.exists(os.path.join(rd, f"tick_{t:04d}.png")):
            return True
        try:
            return time.time() - os.path.getmtime(os.path.join(rd, f"tick_{t:04d}.json")) > 3
        except OSError:
            return False

    def step(delta):
        ts = ticks()
        if not ts:
            return
        if st["cur"] is None:
            show(ts[0])
            return
        nxt = [t for t in ts if t > st["cur"]] if delta > 0 else [t for t in ts if t < st["cur"]][::-1]
        if nxt:
            show(nxt[0])

    def poll():
        try:
            if a.follow:
                nr = V.newest_run(runs_root)
                if nr and os.path.normcase(nr) != os.path.normcase(st["reader"].run_dir):
                    st["reader"], st["cur"] = V.RunReader(nr), None
            ts = ticks()
            if a.replay:
                if not st["paused"] and time.time() >= st["next_t"]:
                    step(+1)
                    st["next_t"] = time.time() + 1.0 / max(a.fps, 0.01)
            elif ts and not st["paused"]:
                for t in reversed(ts[-3:]):
                    if t != st["cur"] and ready(t):
                        if st["cur"] is None or t > st["cur"]:
                            show(t)
                        break
        except Exception as e:   # a half-written file etc.: keep the window alive
            print(f"[viewer] {e}")
        root.after(int(a.poll * 1000) if not a.replay else 50, poll)

    def key(ev):
        k = ev.keysym
        if k in ("q", "Escape"):
            root.destroy()
        elif k == "space":
            st["paused"] = not st["paused"]
        elif k == "Right":
            st["paused"] = True
            step(+1)
        elif k == "Left":
            st["paused"] = True
            step(-1)
        elif k in ("Home", "End"):
            ts = ticks()
            if ts:
                st["paused"] = True
                show(ts[0] if k == "Home" else ts[-1])
    root.bind("<Key>", key)
    if a.replay:
        step(+1)
        st["next_t"] = time.time() + 1.0 / max(a.fps, 0.01)
    root.after(10, poll)
    root.mainloop()


if __name__ == "__main__":
    main()
