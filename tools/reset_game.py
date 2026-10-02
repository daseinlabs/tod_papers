"""reset_game.py -- put Papers, Please into a known clean state for a TOD run.

No scripted in-game clicks. Steps:
  1. terminate PapersPlease.exe (terminate, then kill after 10 s);
  2. copy the whole save folder (%APPDATA%/3909/PapersPlease) to runs/save_backup_<ts>/, then delete the
     progress files (save_*.sav, headers.sav, names.sav, stats.sav). settings.sav (window/video settings),
     steam_autocloud.vdf and screenshots are kept;
  3. relaunch through Steam (steam://rungameid/239030 -- a direct exe launch is restarted by the DRM);
  4. wait for the UnityWndClass window of the new process, then until its frame is non-black and still
     (frame diff), bring it to the foreground and report the client size (expected 2280x1280 = 4x 570x320);
  5. --to-booth: run the normal TOD loop (loop.py) from the main menu with a stop condition: stop, without
     acting, once request 1 says booth_idle/documents_on_desk on 2 consecutive ticks. Every click on the
     way (STORY -> day tile -> NEXT ... -> WALK TO WORK) is TOD's pick.

Run from the repo root with the loop venv:
    .venv-loop\\Scripts\\python.exe tools\\reset_game.py [--to-booth] [--keep-saves] [--booth-ticks 40]
Exit codes: 0 ok, 1 window/launch failure, 3 --to-booth never reached the booth.
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import sys
import time
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from tod_papers import io_win  # noqa: E402  (sets DPI awareness first)
import psutil  # noqa: E402
import win32api  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402

from tod_papers import loop  # noqa: E402

EXE_NAME = "PapersPlease.exe"
STEAM_URL = "steam://rungameid/239030"
SAVE_DIR = os.path.join(os.environ.get("APPDATA", r"%APPDATA%"), "3909", "PapersPlease")
PROGRESS_GLOBS = ("save_*.sav", "stash.sav", "headers.sav", "names.sav", "stats.sav")
NATIVE = (570, 320)
EXPECTED = (2280, 1280)


def log(msg: str) -> None:
    print(f"[reset {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def game_procs() -> list:
    out = []
    for p in psutil.process_iter(["name"]):
        if (p.info.get("name") or "").lower() == EXE_NAME.lower():
            out.append(p)
    return out


def kill_game(timeout: float = 10.0) -> list[int]:
    procs = game_procs()
    pids = [p.pid for p in procs]
    for p in procs:
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
    gone, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        log(f"pid {p.pid} still alive after terminate -> kill")
        p.kill()
    psutil.wait_procs(alive, timeout=5)
    if game_procs():
        raise RuntimeError("PapersPlease.exe still running after kill")
    return pids


def backup_and_clear(ts: str, keep: bool) -> dict:
    info = {"save_dir": SAVE_DIR, "backup": None, "before": [], "removed": [], "after": []}
    if not os.path.isdir(SAVE_DIR):
        log(f"save dir {SAVE_DIR} does not exist (fresh install?)")
        return info
    info["before"] = sorted(os.listdir(SAVE_DIR))
    dst = os.path.join(ROOT, "runs", f"save_backup_{ts}")
    shutil.copytree(SAVE_DIR, dst)
    info["backup"] = dst
    log(f"backed up {len(info['before'])} files -> {dst}")
    if not keep:
        for pat in PROGRESS_GLOBS:
            for f in glob.glob(os.path.join(SAVE_DIR, pat)):
                for i in range(20):   # the killed game can hold names.sav for a moment (WinError 32)
                    try:
                        os.remove(f)
                        break
                    except PermissionError:
                        if i == 19:
                            raise
                        time.sleep(0.5)
                info["removed"].append(os.path.basename(f))
    info["after"] = sorted(os.listdir(SAVE_DIR))
    log(f"removed {info['removed'] or 'nothing'}; kept {[f for f in info['after'] if f.endswith(('.sav', '.vdf'))]}")
    return info


def unity_window_of(pids: set[int]) -> int | None:
    hits = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetClassName(h) == loop.WINDOW_CLASS:
            _, pid = win32process.GetWindowThreadProcessId(h)
            if pid in pids:
                hits.append(h)
        return True

    win32gui.EnumWindows(cb, None)
    return hits[0] if hits else None


def wait_window(timeout: float) -> int:
    t0 = time.time()
    while time.time() - t0 < timeout:
        pids = {p.pid for p in game_procs()}
        if pids:
            h = unity_window_of(pids)
            if h:
                log(f"window hwnd={h} pid(s)={sorted(pids)} after {time.time() - t0:.1f}s")
                return h
        time.sleep(0.5)
    raise TimeoutError(f"no {loop.WINDOW_CLASS} window from {EXE_NAME} within {timeout:.0f}s")


def wait_stable(hwnd: int, timeout: float, need: int = 4, thresh: float = 0.004) -> dict:
    """Foreground the window and grab every 0.5 s until `need` consecutive frame
    pairs differ by < `thresh` (fraction of pixels) and the frame is not black."""
    t0 = time.time()
    grab = None
    prev, run, size = None, 0, None
    try:
        while time.time() - t0 < timeout:
            loop.try_foreground(hwnd)
            r = io_win.client_rect_physical(hwnd)
            if grab is None or r[2:] != size:
                if grab is not None:
                    grab.close()
                grab = io_win.Grabber(hwnd)
                size, prev, run = r[2:], None, 0
            f = grab.grab()
            dark = float(f.mean()) < 4.0
            if prev is not None and not dark:
                frac = loop.changed_frac(loop.change_map(prev, f))
                run = run + 1 if frac < thresh else 0
                if run >= need:
                    return {"stable_after_s": round(time.time() - t0, 1), "frame": f, "client": r}
            prev = f
            time.sleep(0.5)
    finally:
        if grab is not None:
            grab.close()
    raise TimeoutError(f"window never became still within {timeout:.0f}s")


def check_geometry(hwnd: int) -> dict:
    x, y, w, h = io_win.client_rect_physical(hwnd)
    sw, sh = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
    style = win32gui.GetWindowLong(hwnd, -16) & 0xFFFFFFFF
    g = {"client": [x, y, w, h], "screen": [sw, sh], "style": hex(style),
         "fullscreen": (w, h) == (sw, sh), "expected": list(EXPECTED)}
    notes = []
    if (w, h) != EXPECTED:
        notes.append(f"client is {w}x{h}, not {EXPECTED[0]}x{EXPECTED[1]}")
    if w * NATIVE[1] != h * NATIVE[0]:
        notes.append("aspect is not 570:320 -> the game letterboxes; anchors.json fallbacks (scaled by W/570, "
                     "H/320) and the 4x-native extract path would be offset")
    elif w % NATIVE[0]:
        notes.append(f"{w}/570 is not an integer scale; extract falls back to a non-native path")
    if g["fullscreen"]:
        notes.append("window covers the whole screen (fullscreen)")
    g["notes"] = notes
    return g


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--to-booth", action="store_true", help="after the relaunch, let TOD (loop.py) play from the "
                    "menu to the Day 1 booth and stop there")
    ap.add_argument("--keep-saves", action="store_true", help="back up but do not delete progress files")
    ap.add_argument("--no-kill", action="store_true", help="do not restart the game (only wait/foreground)")
    ap.add_argument("--window-timeout", type=float, default=120)
    ap.add_argument("--stable-timeout", type=float, default=90)
    ap.add_argument("--booth-ticks", type=int, default=40)
    args = ap.parse_args(argv)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    if not args.no_kill:
        pids = kill_game()
        log(f"terminated {pids or 'nothing (game not running)'}")
        time.sleep(2.0)   # let Steam register the exit (and finish its cloud sync)
        backup_and_clear(ts, args.keep_saves)
        log(f"launching {STEAM_URL}")
        os.startfile(STEAM_URL)
    try:
        hwnd = wait_window(args.window_timeout)
    except TimeoutError as e:
        log(f"FAILED: {e}")
        return 1
    try:
        st = wait_stable(hwnd, args.stable_timeout)
    except TimeoutError as e:   # an animated menu is not fatal: the loop waits for stills itself
        log(f"WARNING: {e}; continuing")
        st = {"stable_after_s": None}
    hwnd = unity_window_of({p.pid for p in game_procs()}) or hwnd
    moved = io_win.ensure_onscreen(hwnd)
    if moved is not None:
        log(f"client was partly off-screen; moved it to {moved}")
    fg = loop.try_foreground(hwnd)
    geo = check_geometry(hwnd)
    log(f"stable after {st['stable_after_s']}s; foreground={fg}; client={geo['client']} style={geo['style']}")
    for n in geo["notes"]:
        log("NOTE: " + n)
    if args.no_kill is False:
        left = sorted(os.listdir(SAVE_DIR)) if os.path.isdir(SAVE_DIR) else []
        log(f"save dir after launch: {left}")
    if not args.to_booth:
        return 0

    log("--to-booth: running the TOD loop from the menu until request 1 says booth (2 consecutive ticks)")
    res: dict = {}
    rc = loop.main(["--max-ticks", str(args.booth_ticks), "--stop-on-screen", "booth_idle,documents_on_desk,stamp_tray_open",
                    "--stop-consecutive", "2", "--save-raw"], result=res)
    log(f"loop rc={rc} stop={res.get('stop_reason')} ticks={res.get('ticks')} run={res.get('run_dir')}")
    return 0 if rc == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
