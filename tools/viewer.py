"""Behind-the-scenes window for screen recording (docs/viewer.md).

  python tools/viewer.py --run runs/<ts>            live: show each new tick as the loop writes it
  python tools/viewer.py --follow                   live: newest run dir, switch when a new run starts
  python tools/viewer.py --run runs/<ts> --replay --fps 2 [--start 130 --end 140]
  python tools/viewer.py --run runs/<ts> --export out_dir [--start N --end M]   (PNGs, no window)
  python tools/viewer.py --follow --layout video     the demo-video sidebar (animated tick-to-tick transitions)
  python tools/viewer.py --run runs/<ts> --layout video --export-video out.mp4 [--hold 0.5 --start N --end M]
                                                     composite time-lapse mp4 (raw game frame | panel), via ffmpeg

Read-only: polls the run dir, never touches the loop or the game; runs at below-normal priority and never
takes focus after start. Keys: space pause, Left/Right step, Home/End first/last, q/Esc quit.
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

    def cb(h, _):   # same match as loop.find_game_window: the Unity window, title "PapersPlease" preferred
        if win32gui.IsWindowVisible(h) and win32gui.GetClassName(h) == "UnityWndClass":
            found.append((win32gui.GetWindowText(h) != "PapersPlease", h))
        return True
    win32gui.EnumWindows(cb, None)
    found = [f for f in found if not win32gui.IsIconic(f[1])]   # minimized windows sit at (-32000, -32000)
    if not found:
        return None
    h = sorted(found)[0][1]
    l, t, r, b = win32gui.GetClientRect(h)   # client rect in screen px (borderless: == window rect)
    sx, sy = win32gui.ClientToScreen(h, (0, 0))
    return sx, sy, sx + (r - l), sy + (b - t)


def display_rect():
    """(l, t, r, b) of the primary display in physical px (call after SetProcessDpiAwareness)."""
    try:
        u = ctypes.windll.user32
        return 0, 0, u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    except Exception:
        return 0, 0, 3456, 2160


def below_normal_priority():
    """The viewer must never compete with the loop / the game for CPU."""
    try:
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k.SetPriorityClass(k.GetCurrentProcess(), 0x4000)   # BELOW_NORMAL_PRIORITY_CLASS
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="run dir (runs/<ts>)")
    ap.add_argument("--follow", action="store_true", help="use the newest run dir under runs/, switch to newer ones")
    ap.add_argument("--replay", action="store_true", help="play a finished run from --start at --fps")
    ap.add_argument("--fps", type=float, default=1.0, help="replay ticks per second")
    ap.add_argument("--start", type=int, default=None)
    ap.add_argument("--end", type=int, default=None)
    ap.add_argument("--export", help="write panel PNGs to this dir and exit (no window)")
    ap.add_argument("--layout", choices=("debug", "video"), default="debug",
                    help="debug: the dense portrait sheet; video: the demo sidebar with transitions")
    ap.add_argument("--export-video", help="video layout: write a composite time-lapse mp4 (raw game | panel) "
                    "and exit")
    ap.add_argument("--hold", type=float, default=0.5, help="export-video: seconds per tick (>= 0.45 shows the "
                    "full transition)")
    ap.add_argument("--video-fps", type=int, default=30, help="export-video frame rate")
    ap.add_argument("--scale", type=float, default=1.0, help="export-video output scale (1.0 = 3456x1944 with "
                    "the default panel, game top-left, black below it; 0.5 for a small file)")
    ap.add_argument("--panel-only", action="store_true", help="export-video without the raw game frame")
    ap.add_argument("--width", type=int, default=None, help="panel width, physical px (debug 1160; video: display "
                    "width minus the game window's right edge, 1176 on 3456x2160 with the game at x=0)")
    ap.add_argument("--height", type=int, default=None, help="panel height (debug 1980; video: 1944 = three stacked "
                    "1176x648 tiles for the Cap zoom plan; 1280 = game height, 2160 = full column)")
    ap.add_argument("--x", type=int, default=None, help="window left (physical px); default: right of the game")
    ap.add_argument("--y", type=int, default=None, help="window top; default: the game's top (video) / 0 (debug)")
    ap.add_argument("--poll", type=float, default=0.25, help="live poll interval, s")
    ap.add_argument("--anim-fps", type=float, default=60, help="video layout: redraw rate during a transition")
    ap.add_argument("--obs", action="store_true", help="video layout in a NORMAL top-level window titled exactly "
                    "\"TOD viewer\" (captioned, in the taskbar, not topmost) for an OBS Window Capture source; "
                    "content 1176x1944 (--width/--height override)")
    a = ap.parse_args()
    if a.obs:
        a.layout = "video"
        a.width = a.width or 1176
        a.height = a.height or 1944

    runs_root = os.path.join(ROOT, "runs")
    run_dir = a.run if a.run else V.newest_run(runs_root)
    if not run_dir or not os.path.isdir(run_dir):
        sys.exit(f"no run dir ({run_dir})")
    reader = V.RunReader(run_dir)
    video = a.layout == "video" or bool(a.export_video)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # physical pixels, crisp text at 225 % scaling
    except Exception:
        pass

    # ---- geometry: the panel fills the display to the right of the game window, no overlap
    gr = game_rect() if not (a.export or a.export_video) else None
    dl, dt, dr, db = display_rect()
    if gr and (gr[2] - gr[0] < 400 or gr[3] - gr[1] < 200 or gr[2] <= dl or gr[0] >= dr):
        gr = None   # not a usable game window on this display
    if video:
        from tod_papers import viewer_video as VV
        # the panel takes the display width to the right of the game, at the game's top and height;
        # without a game window: the right (display - 2280) px of the display; top-aligned, 1944 high by default
        room = (dr - gr[2]) if gr else (dr - VV.GAME_W)
        width = a.width or (room if room >= 480 else VV.DEFAULT_W)
        height = a.height or VV.DEFAULT_H          # 1944 = three 1176x648 tiles (Cap zoom plan)
        x = a.x if a.x is not None else (gr[2] if gr and room >= 480 else dr - width)
        y = a.y if a.y is not None else (gr[1] if gr else 0)
    else:
        width, height = a.width or 1160, a.height or 1980
        x = a.x if a.x is not None else ((gr[2] + 4) if gr else 2290)
        y = a.y if a.y is not None else (gr[1] if gr else 0)

    if a.export_video:
        vp = VV.VideoPanel(width, height)
        ts = [t for t in V.list_ticks(run_dir)
              if (a.start is None or t >= a.start) and (a.end is None or t <= a.end)]
        t0 = time.time()
        info = VV.export_video(reader, vp, ts, a.export_video, hold=a.hold, fps=a.video_fps, scale=a.scale,
                               with_game=not a.panel_only)
        print(f"[export-video] {info['out']}: {info['size'][0]}x{info['size'][1]} {info['frames']} frames "
              f"{info['seconds']:.1f}s ({len(ts)} ticks, {time.time() - t0:.0f}s to render)")
        return

    if a.export:
        os.makedirs(a.export, exist_ok=True)
        if video:
            vp = VV.VideoPanel(width, height)
        else:
            panel = V.Panel(width, height)
        for t in V.list_ticks(run_dir):
            if (a.start is not None and t < a.start) or (a.end is not None and t > a.end):
                continue
            if video:
                sc = VV.build_scene(reader, t, vp.som_size)
                im = vp.draw(sc) if sc else None
            else:
                im = V.render_tick(reader, panel, t, "export")
            if im is not None:
                im.save(os.path.join(a.export, f"view_{t:04d}.png"))
        print(f"exported to {a.export}")
        return

    below_normal_priority()
    import tkinter as tk
    from PIL import ImageTk

    root = tk.Tk()
    root.title("TOD viewer")
    root.configure(bg="#121418")
    if video and not a.obs:
        root.overrideredirect(True)   # borderless: the panel sits flush against the borderless game window
    if a.obs:
        root.resizable(False, False)   # normal captioned window (OBS's window list skips tool windows); client = WxH
    root.geometry(f"{width}x{height}+{x}+{y}")
    lab = tk.Label(root, bg="#0e1014", bd=0, highlightthickness=0)
    lab.pack(fill="both", expand=True)
    if video:
        print(f"[viewer] video layout {width}x{height} at ({x},{y}); display {dr}x{db}; "
              f"game {'%dx%d at (%d,%d)' % (gr[2] - gr[0], gr[3] - gr[1], gr[0], gr[1]) if gr else 'not found'}")
        vp = VV.VideoPanel(width, height)
    else:
        panel = V.Panel(width, height)

    st = {"reader": reader, "cur": None, "paused": False, "photo": None, "next_t": time.time(),
          "scene": None, "prev": None, "t0": 0.0, "anim": False}
    mode = "replay" if a.replay else ("follow" if a.follow else "live")

    def blit(im):
        st["photo"] = ImageTk.PhotoImage(im)
        lab.configure(image=st["photo"])

    def animate():
        """Video layout: redraw at --anim-fps until the transition is done, then stay idle (a held tick costs
        nothing). Time is wall-clock, so a slow redraw drops frames instead of slowing the motion."""
        if st["scene"] is None:
            st["anim"] = False
            return
        t = time.time() - st["t0"]
        blit(vp.draw(st["scene"], st["prev"], t))
        if t < VV.ANIM_END + 0.3:
            root.after(max(1, int(1000 / a.anim_fps)), animate)
        else:
            st["anim"] = False

    def show(t):
        if video:
            sc = VV.build_scene(st["reader"], t, vp.som_size)
            if sc is None:
                return False
            st["prev"] = st["scene"] if st["scene"] is not None and abs(t - st["scene"].tick) <= 3 else None
            st["scene"], st["t0"] = sc, time.time()
            if not st["anim"]:
                st["anim"] = True
                animate()
        else:
            im = V.render_tick(st["reader"], panel, t, mode)
            if im is None:
                return False
            blit(im)
        st["cur"] = t
        if not a.obs:   # --obs: the title stays exactly "TOD viewer" (OBS matches the window by title)
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
                    st["reader"], st["cur"], st["scene"] = V.RunReader(nr), None, None
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
    if video:   # borderless windows get no keyboard focus by default: click the panel once to use the keys
        lab.bind("<Button-1>", lambda e: root.focus_force())
    if a.replay:
        step(+1)
        st["next_t"] = time.time() + 1.0 / max(a.fps, 0.01)
    root.after(10, poll)
    root.mainloop()


if __name__ == "__main__":
    main()
