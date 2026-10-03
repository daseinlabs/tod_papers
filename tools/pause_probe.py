"""Measure harness pause options on a running Papers, Please (no gameplay input).

    .venv-loop\\Scripts\\python tools\\pause_probe.py                    # all tests, game on a DayScreen
    .venv-loop\\Scripts\\python tools\\pause_probe.py --tests suspend --hold 3 6
    .venv-loop\\Scripts\\python tools\\pause_probe.py --tests suspend --input   # also Esc-test input on resume

Tests (each prints one line and goes into the --out JSON):
  run      baseline: game-clock advance (gt) and on-screen frame change over --run-s with nothing done
  suspend  io_win.suspend_game() for each --hold seconds: does the gt clock stop, does the frame stay
           pixel-identical, does Windows flag the window as hung / ghost it, how fast does it come back
           (WM_NULL round trip, first frame change, first clock tick after resume)
  trace    the gt clock sampled at ~50 Hz around each --hold suspend (rate, advance while suspended,
           jump right after resume)
  esc      Esc -> pause-menu latency: baseline vs right after a resume (opens and closes the game's menu)
  focus    a blank tkinter window takes the foreground for --focus-s: does the clock stop (Unity
           "Run In Background" off) and is the game frame still presented
--input sends Esc right after a resume (opens the game's pause menu, then Esc closes it) to time how soon
the game reacts to input; only on DayScreen and only with the game in the foreground.
Ground truth (gt.snapshot) is read-only and used for measurement only. Frames are compared numerically,
never saved. The game is always resumed, also on Ctrl-C (io_win registers an atexit resume).
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from tod_papers import io_win  # noqa: E402  (DPI awareness first)
import numpy as np  # noqa: E402
import psutil  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402

from tod_papers import gt  # noqa: E402

EXE = "PapersPlease.exe"
PIX = 24            # per-channel level change that counts as a changed pixel
MENU_FRAC = 0.05    # same threshold loop.py uses for "pause menu on screen"


def find_game() -> tuple[int, int]:
    pids = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == EXE.lower()}
    hits = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetClassName(h) == "UnityWndClass":
            if win32process.GetWindowThreadProcessId(h)[1] in pids:
                hits.append(h)
        return True

    win32gui.EnumWindows(cb, None)
    if not hits:
        raise SystemExit(f"no visible UnityWndClass window of {EXE}")
    return hits[0], io_win.window_pid(hits[0])


def diff(a: np.ndarray, b: np.ndarray) -> dict:
    a, b = a[::2, ::2].astype(np.int16), b[::2, ::2].astype(np.int16)
    d = np.abs(a - b).max(axis=2)
    return {"frac": round(float((d > PIX).mean()), 5), "mean": round(float(d.mean()), 3), "max": int(d.max())}


def gmin(s: dict) -> float | None:
    """Game clock in minutes, from the console clock's float hour (finer than the HH:MM string)."""
    h = s.get("clock_hour") if s.get("ok") else None
    return round(h * 60, 3) if isinstance(h, (int, float)) else None


def snap() -> dict:
    s = gt.snapshot()
    return {"t": time.perf_counter(), "ok": s.get("ok"), "screen": s.get("screen"), "day": s.get("day"),
            "clock": s.get("clock"), "m": gmin(s)}


def wm_null_ms(hwnd: int, timeout_ms: int = 3000) -> float | None:
    """Round trip of a WM_NULL through the game's message queue (None = timed out)."""
    res = ctypes.c_size_t()
    t0 = time.perf_counter()
    ok = ctypes.windll.user32.SendMessageTimeoutW(hwnd, 0, 0, 0, 0x0002, timeout_ms, ctypes.byref(res))
    return round((time.perf_counter() - t0) * 1e3, 1) if ok else None


def ghost_present() -> bool:
    """A visible DWM 'Ghost' window (what Windows shows in place of a hung window the user clicks)."""
    found = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetClassName(h) == "Ghost":
            found.append(h)
        return True

    win32gui.EnumWindows(cb, None)
    return bool(found)


def proc_state(pid: int) -> dict:
    p = psutil.Process(pid)
    mods = []
    try:
        mods = [os.path.basename(m.path).lower() for m in p.memory_maps()]
    except Exception:
        pass
    return {"status": p.status(), "threads": p.num_threads(),
            "steam_overlay_dll": any("gameoverlayrenderer" in m for m in mods)}


def wait_clock_move(m0: int | None, timeout: float) -> float | None:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        s = snap()
        if s["m"] is not None and m0 is not None and s["m"] != m0:
            return round(s["t"] - t0, 3)
        time.sleep(0.02)
    return None


def test_run(hwnd, pid, grab, args) -> dict:
    a, s0 = grab.grab(), snap()
    time.sleep(args.run_s)
    b, s1 = grab.grab(), snap()
    dt = s1["t"] - s0["t"]
    dm = (s1["m"] - s0["m"]) if None not in (s0["m"], s1["m"]) else None
    return {"test": "run", "s": round(dt, 2), "clock": [s0["clock"], s1["clock"]], "game_min": dm,
            "game_min_per_s": round(dm / dt, 2) if dm is not None else None, "frame": diff(a, b),
            "screen": s0["screen"]}


def test_suspend(hwnd, pid, grab, args, hold: float) -> dict:
    a, s0 = grab.grab(), snap()
    t0 = time.perf_counter()
    io_win.suspend_game(pid=pid, keepalive_s=args.keepalive or None)
    t_susp = (time.perf_counter() - t0) * 1e3
    samples = []
    hung_any = False
    while time.perf_counter() - t0 < hold:
        time.sleep(min(1.0, max(0.0, hold - (time.perf_counter() - t0))))
        hung_any |= io_win.window_hung(hwnd)
        f = grab.grab()
        samples.append({"t": round(time.perf_counter() - t0, 2), "clock": snap()["clock"], **diff(a, f)})
    hung_any |= io_win.window_hung(hwnd)
    st = proc_state(pid)
    ghost = ghost_present()
    title = win32gui.GetWindowText(hwnd)
    b, s1 = grab.grab(), snap()
    held = time.perf_counter() - t0
    t1 = time.perf_counter()
    io_win.resume_game(pid)
    t_res = (time.perf_counter() - t1) * 1e3
    null_ms = wm_null_ms(hwnd)
    input_ms = None
    if args.input and s1["screen"] == "DayScreen" and win32gui.GetForegroundWindow() == hwnd:
        ref = grab.grab()
        tk = time.perf_counter()
        io_win.key(io_win.VK_ESCAPE)
        while time.perf_counter() - tk < 2.0:
            if diff(ref, grab.grab())["frac"] >= MENU_FRAC:
                input_ms = round((time.perf_counter() - tk) * 1e3)
                break
            time.sleep(0.02)
        io_win.key(io_win.VK_ESCAPE)   # close the menu again
        time.sleep(0.6)
    # first visible change after resume (the booth has ambient motion) and first clock tick
    t2 = time.perf_counter()
    first_change = None
    while time.perf_counter() - t2 < 2.0:
        if diff(b, grab.grab())["frac"] > 0.0005:
            first_change = round((time.perf_counter() - t2) * 1e3)
            break
        time.sleep(0.01)
    clock_ms = wait_clock_move(s1["m"], 3.0)
    return {"test": "suspend", "hold_s": round(held, 2), "suspend_ms": round(t_susp, 2),
            "resume_ms": round(t_res, 2), "clock": [s0["clock"], s1["clock"]],
            "clock_stopped": s0["m"] == s1["m"],
            "game_min": (round(s1["m"] - s0["m"], 3) if None not in (s0["m"], s1["m"]) else None), "frame_during": samples, "frame_end": diff(a, b),
            "hung": hung_any, "keepalive_s": args.keepalive, "ghost": ghost, "title": title, "proc": st,
            "after_resume": {"wm_null_ms": null_ms, "esc_menu_ms": input_ms,
                             "first_frame_change_ms": first_change,
                             "first_clock_tick_s": clock_ms, "hung": io_win.window_hung(hwnd),
                             "status": psutil.Process(pid).status()}}


def test_trace(hwnd, pid, grab, args, hold: float) -> dict:
    """Clock at ~50 Hz: --pre-s running, `hold` s suspended, 2 s after resume. Gives the running rate, the
    advance while suspended (incl. keep-alive thaws) and the jump in the first frames after resume."""
    pts = []

    def sample(until: float, phase: str):
        while time.perf_counter() < until:
            s = snap()
            pts.append((s["t"], s["m"], phase))
            time.sleep(0.02)

    t = time.perf_counter()
    sample(t + 2.0, "run")
    t_s = time.perf_counter()
    io_win.suspend_game(pid=pid, keepalive_s=args.keepalive or None)
    sample(t_s + hold, "susp")
    t_r = time.perf_counter()
    io_win.resume_game(pid)
    sample(t_r + 2.0, "after")
    run = [p for p in pts if p[2] == "run" and p[1] is not None]
    rate = (run[-1][1] - run[0][1]) / (run[-1][0] - run[0][0]) if len(run) > 1 else None   # game-min per s
    before = run[-1][1] if run else None
    sus = [p for p in pts if p[2] == "susp" and p[1] is not None]
    aft = [p for p in pts if p[2] == "after" and p[1] is not None]
    adv_susp = (sus[-1][1] - before) if sus and before is not None else None
    j = [p for p in aft if p[0] - t_r <= 0.25]
    jump = (j[-1][1] - sus[-1][1]) if j and sus else None
    tail = [p for p in aft if p[0] - t_r >= 0.5]
    rate_after = (tail[-1][1] - tail[0][1]) / (tail[-1][0] - tail[0][0]) if len(tail) > 1 else None
    first_move = next((round(p[0] - t_s, 3) for p in sus if before is not None and p[1] != before), None)
    return {"test": "trace", "hold_s": round(t_r - t_s, 2), "keepalive_s": args.keepalive,
            "rate_game_min_per_s": round(rate, 3) if rate else rate,
            "advance_while_suspended_min": round(adv_susp, 3) if adv_susp is not None else None,
            "expected_if_running_min": round(rate * (t_r - t_s), 2) if rate else None,
            "first_move_while_suspended_s": first_move,
            "jump_first_250ms_after_resume_min": round(jump, 3) if jump is not None else None,
            "rate_after_resume": round(rate_after, 3) if rate_after else rate_after}


def esc_ms(grab) -> int | None:
    ref = grab.grab()
    tk = time.perf_counter()
    io_win.key(io_win.VK_ESCAPE)
    out = None
    while time.perf_counter() - tk < 3.0:
        if diff(ref, grab.grab())["frac"] >= MENU_FRAC:
            out = round((time.perf_counter() - tk) * 1e3)
            break
        time.sleep(0.02)
    io_win.key(io_win.VK_ESCAPE)
    time.sleep(0.8)
    return out


def test_esc(hwnd, pid, grab, args) -> dict:
    """Input latency (Esc -> pause menu visible): baseline, then right after resumes of 1 s and 4 s holds."""
    out = {"test": "esc", "baseline_ms": esc_ms(grab)}
    for h in (1.0, 4.0):
        io_win.suspend_game(pid=pid, keepalive_s=args.keepalive or None)
        time.sleep(h)
        io_win.resume_game(pid)
        out[f"after_{h:g}s_ms"] = esc_ms(grab)
    out["baseline2_ms"] = esc_ms(grab)
    return out


BLANK = ("import tkinter as t; r=t.Tk(); r.title('pause_probe blank'); r.geometry('360x240+40+40');"
         "r.configure(bg='white'); r.after({ms}, r.destroy); r.lift(); r.focus_force(); r.mainloop()")


def test_focus(hwnd, pid, grab, args) -> dict:
    ms = int((args.focus_s + 3) * 1000)
    proc = subprocess.Popen([sys.executable, "-c", BLANK.format(ms=ms)])
    blank = None
    t0 = time.perf_counter()
    while blank is None and time.perf_counter() - t0 < 5:
        time.sleep(0.1)
        blank = win32gui.FindWindow(None, "pause_probe blank") or None
    for _ in range(6):
        if blank:
            io_win.focus(blank)
        time.sleep(0.25)
        if blank and win32gui.GetForegroundWindow() == blank:
            break
    fg = win32gui.GetForegroundWindow()
    fg_ok = fg != hwnd and io_win.window_pid(fg) != pid   # the game is not foreground (blank or anything else)
    a, s0 = grab.grab(), snap()
    time.sleep(args.focus_s)
    b, s1 = grab.grab(), snap()
    dt = s1["t"] - s0["t"]
    dm = (s1["m"] - s0["m"]) if None not in (s0["m"], s1["m"]) else None
    proc.terminate()
    proc.wait(5)
    io_win.focus(hwnd)
    time.sleep(0.3)
    return {"test": "focus", "blank_foreground": fg_ok, "s": round(dt, 2), "clock": [s0["clock"], s1["clock"]],
            "game_min": dm, "game_min_per_s": round(dm / dt, 2) if dm is not None else None,
            "frame": diff(a, b), "game_foreground_again": win32gui.GetForegroundWindow() == hwnd}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tests", nargs="+", default=["run", "suspend", "focus", "run"])
    ap.add_argument("--hold", type=float, nargs="+", default=[3.0, 6.0, 10.0], help="suspend durations (s)")
    ap.add_argument("--run-s", type=float, default=5.0)
    ap.add_argument("--focus-s", type=float, default=5.0)
    ap.add_argument("--keepalive", type=float, default=3.0, help="suspend keep-alive period (0 = off)")
    ap.add_argument("--input", action="store_true", help="Esc-test input right after each resume")
    ap.add_argument("--out", default=None, help="JSON results file")
    args = ap.parse_args(argv)

    hwnd, pid = find_game()
    io_win.focus(hwnd)
    time.sleep(0.3)
    grab = io_win.Grabber(hwnd)
    results = {"pid": pid, "proc": proc_state(pid), "start": snap(), "tests": []}
    print(f"[probe] pid={pid} {results['start']['screen']} day={results['start']['day']} "
          f"clock={results['start']['clock']} overlay_dll={results['proc']['steam_overlay_dll']}")
    try:
        for name in args.tests:
            if name == "suspend":
                for h in args.hold:
                    r = test_suspend(hwnd, pid, grab, args, h)
                    results["tests"].append(r)
                    ar = r["after_resume"]
                    print(f"[suspend {r['hold_s']:.1f}s] clock {r['clock'][0]}->{r['clock'][1]} ({r['game_min']} game-min) "
                          f"frame frac={r['frame_end']['frac']} max={r['frame_end']['max']} hung={r['hung']} "
                          f"ghost={r['ghost']} status={r['proc']['status']} | resume {r['resume_ms']}ms "
                          f"wm_null={ar['wm_null_ms']}ms esc={ar['esc_menu_ms']}ms "
                          f"1st change={ar['first_frame_change_ms']}ms clock tick={ar['first_clock_tick_s']}s")
                    time.sleep(1.0)
            elif name == "trace":
                for h in args.hold:
                    r = test_trace(hwnd, pid, grab, args, h)
                    results["tests"].append(r)
                    print(f"[trace {r['hold_s']}s ka={r['keepalive_s']}] rate {r['rate_game_min_per_s']} game-min/s, "
                          f"advance while suspended {r['advance_while_suspended_min']} min (running would be "
                          f"{r['expected_if_running_min']}), first move {r['first_move_while_suspended_s']}s, "
                          f"jump after resume {r['jump_first_250ms_after_resume_min']} min")
                    time.sleep(0.5)
            elif name == "esc":
                r = test_esc(hwnd, pid, grab, args)
                results["tests"].append(r)
                print(f"[esc] {r}")
            elif name == "run":
                r = test_run(hwnd, pid, grab, args)
                results["tests"].append(r)
                print(f"[run {r['s']}s] {r['screen']} clock {r['clock'][0]}->{r['clock'][1]} "
                      f"({r['game_min_per_s']} game-min/s) frame frac={r['frame']['frac']}")
            elif name == "focus":
                r = test_focus(hwnd, pid, grab, args)
                results["tests"].append(r)
                print(f"[focus {r['s']}s] blank fg={r['blank_foreground']} clock {r['clock'][0]}->{r['clock'][1]} "
                      f"({r['game_min_per_s']} game-min/s) frame frac={r['frame']['frac']} "
                      f"game fg again={r['game_foreground_again']}")
            else:
                print(f"[probe] unknown test {name!r}")
    finally:
        io_win.resume_game()
        grab.close()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
