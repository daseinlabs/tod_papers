r"""demo_run.py -- the TOD Papers, Please demo in one command: cold start -> Day 1 -> Day 2 -> Day 3 -> Day 3 night.

    .venv-loop\Scripts\python.exe tools\demo_run.py                  # fresh story (saves wiped), un-paused
    .venv-loop\Scripts\python.exe tools\demo_run.py --pause-think    # game clock held while TOD thinks
    .venv-loop\Scripts\python.exe tools\demo_run.py --from-day 3     # retake: keep saves, TOD picks the day tile

Steps (no scripted in-game clicks; every click from the title screen on is TOD's pick):
  1. terminate the game; back up the save folder to runs/save_backup_<ts>/; unless --from-day, delete the story
     progress files (tools/reset_game.py, same file list) so DAY 1 is the only tile ("NEW"); remove
     runs/LAST_DAY.json (TOD reads the day again from the screen);
  2. relaunch through Steam, wait for the window, wait until its frame is still, keep it on-screen
     (io_win.ensure_onscreen) and log the client rect to runs/demo_<ts>.json (for framing the recording);
  3. check game-memory ground truth (gt) attaches -- the demo STOP needs it;
  4. ensure a remote-extract tunnel, A100 first: GET 127.0.0.1:8766/health, if down start
     `deploy/gcp_l4_spot.sh tunnel --a100` detached; if it does not answer, fall back to the L4 (8765,
     `deploy/gcp_l4_spot.sh tunnel`). Logs runs/tunnel_<ts>_<a100|l4>.log, records the one used in
     runs/demo_<ts>.json ("extractor"); never starts/stops instances. TOD_EXTRACT_URL is set in the loop's env only;
  5. run ONE `python -m tod_papers.loop --max-ticks 1500 --save-raw --stop-on-gt-day-end 3 ...` from the title
     screen. It stops on the first tick where gt says day >= 3 and screen NightScreen (gt is used for that stop
     only, never in TOD's text). `--stop-on-screen day_end` is NOT used (it would stop at Day 1's night);
  6. print tools/report.py for the run.
Exit codes: 0 Day 3 night reached, 1 launch / window / gt / tunnel failure, 4 loop stopped elsewhere.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import reset_game as rg  # noqa: E402  (imports io_win first: DPI awareness)
import report  # noqa: E402
from tod_papers import io_win  # noqa: E402

DEFAULT_URL = None   # None: A100 tunnel (8766) first, then the L4 (8765); see EXTRACTORS
FAST_URL = "http://127.0.0.1:8790"   # --fast: tod-fast scorer box, `deploy/gcp_fast.sh tunnel`
STOP_FLAGS = ["--stall-stop", "12", "--pick-stop", "10", "--refuse-stop", "5"]   # the loop's usual safety stops


def log(msg: str) -> None:
    print(f"[demo {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def health(url: str, timeout: float = 5.0) -> dict | None:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def find_bash() -> str | None:
    for c in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
        if c and os.path.exists(c) and "system32" not in c.lower():   # not WSL's bash.exe
            return c
    return None


# Remote extractors in preference order: (name, local tunnel url, deploy/gcp_l4_spot.sh args). The A100 box
# (tod-extract-a100) tunnels on 8766, the L4 on 8765. Only tunnels are started here -- never an instance.
EXTRACTORS = [("A100", "http://127.0.0.1:8766", ["tunnel", "--a100"]),
              ("L4", "http://127.0.0.1:8765", ["tunnel"])]


def ensure_tunnel(url: str, ts: str, wait_s: float, tunnel_args: list[str] | None = None,
                  name: str = "", sh: str = "deploy/gcp_l4_spot.sh") -> bool:
    tunnel_args = tunnel_args or ["tunnel"]
    script = sh + " " + " ".join(tunnel_args)
    h = health(url)
    if h:
        log(f"extract server {name} up at {url} (warm={h.get('warm')} gpu={h.get('gpu')})")
        return True
    bash = find_bash()
    if not bash:
        log(f"extract server {name} down and Git Bash not found; start `{script}` yourself")
        return False
    lp = os.path.join(ROOT, "runs", f"tunnel_{ts}{'_' + name.lower() if name else ''}.log")
    log(f"extract server {name} down at {url} -> starting `{script}` detached (log {lp})")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    with open(lp, "w", encoding="utf-8") as fh:
        proc = subprocess.Popen([bash, sh] + tunnel_args, cwd=ROOT, stdout=fh,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags,
                                close_fds=True)
    t0 = time.time()
    while time.time() - t0 < wait_s:
        time.sleep(3.0)
        h = health(url)
        if h:
            log(f"tunnel {name} up after {time.time() - t0:.0f}s (warm={h.get('warm')} gpu={h.get('gpu')})")
            return True
        if proc.poll() is not None:   # ssh exited (VM stopped / no IP): no point waiting the full budget
            log(f"tunnel {name} exited (rc {proc.returncode}) after {time.time() - t0:.0f}s; see {lp}")
            return False
    log(f"no /health at {url} within {wait_s:.0f}s. Is the VM running? "
        f"(`{sh} status{' --a100' if '--a100' in tunnel_args else ''}`)")
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)   # no stray ssh
    return False


def ensure_extractor(ts: str, wait_s: float, url: str | None = None) -> tuple[str, str] | None:
    """Prefer the A100 tunnel (8766), fall back to the L4 (8765). An explicit --extract-url is used alone.
    Returns (name, url) of the extractor that answered /health, or None."""
    if url:
        return ("custom", url) if ensure_tunnel(url, ts, wait_s, name="custom") else None
    for name, u, targs in EXTRACTORS:
        if ensure_tunnel(u, ts, wait_s, targs, name):
            return name, u
        log(f"{name} extractor unavailable -> trying the next one")
    return None


def wait_gt(timeout: float) -> dict:
    from tod_papers import gt
    t0, s = time.time(), {}
    while time.time() - t0 < timeout:
        s = gt.snapshot()
        if s.get("ok"):
            return s
        time.sleep(2.0)
    return s


def place_window(hwnd: int, place: str) -> tuple:
    """Move the window so its client origin is exactly at "X,Y" (physical px); returns the new origin."""
    px, py = (int(v) for v in place.split(","))
    for _ in range(5):
        cx, cy, _, _ = io_win.client_rect_physical(hwnd)
        if (cx, cy) == (px, py):
            break
        wl, wt, _, _ = io_win.win32gui.GetWindowRect(hwnd)
        io_win.win32gui.SetWindowPos(hwnd, 0, wl + (px - cx), wt + (py - cy), 0, 0, 0x0001 | 0x0004 | 0x0010)
        time.sleep(0.5)   # SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
    moved = io_win.client_rect_physical(hwnd)[:2]
    log(f"--place {px},{py}: client origin now {moved}")
    return moved


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pause-think", action="store_true", help="pass --pause-think to the loop (off by default)")
    ap.add_argument("--from-day", type=int, choices=(1, 2, 3), default=None,
                    help="retake: keep the saves; TOD picks the day tile on the day-select screen (the manual tells "
                         "it to pick the HIGHEST tile, so the saves' highest day must be N); the run still ends at "
                         "Day 3's night")
    ap.add_argument("--max-ticks", type=int, default=1500)
    ap.add_argument("--stop-day", type=int, default=3, help="stop at this day's night screen (gt)")
    ap.add_argument("--extract-url", default=DEFAULT_URL, help="use only this extractor url (default: A100 tunnel 8766, else L4 8765)")
    ap.add_argument("--local-extract", action="store_true", help="do not use the remote extractor (slower ticks)")
    ap.add_argument("--tunnel-wait", type=float, default=90)
    ap.add_argument("--no-launch", action="store_true", help="the game is already on the title screen; do not "
                    "kill / wipe / relaunch")
    ap.add_argument("--window-timeout", type=float, default=120)
    ap.add_argument("--stable-timeout", type=float, default=90)
    ap.add_argument("--wipe-saves", action="store_true",
                    help="delete the story progress files before launch (opt-in; Steam Cloud may restore them)")
    ap.add_argument("--place", default=None, metavar="X,Y",
                    help="move the game so its client origin is at this physical screen px (recording: 0,0)")
    ap.add_argument("--fast", action="store_true",
                    help="score on the tod-fast box (shared-prefix scorer, deploy/gcp_fast.sh): bring its tunnel up on "
                         f"{FAST_URL}, health-check it, set TOD_API_URL for the loop. Never starts the VM.")
    ap.add_argument("--loop-args", default="", help="extra loop.py flags, e.g. \"--history 20\"")
    args = ap.parse_args(argv)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    meta: dict = {"ts": ts, "pause_think": args.pause_think, "from_day": args.from_day}

    # ---- 1-2: saves + launch -----------------------------------------------------------------------------------
    if not args.no_launch:
        log(f"terminated {rg.kill_game() or 'nothing (game not running)'}")
        time.sleep(2.0)   # let Steam register the exit (and finish its cloud sync)
        # saves are KEPT by default (backed up anyway): TOD starts a new story from the game's own menu. --wipe-saves
        # deletes the progress files, but with Steam running Steam Cloud downloads them back at launch
        # (run 20261004_145643 came back to Day 3), so it is opt-in and not used by the demo.
        wipe = args.wipe_saves and args.from_day is None
        meta["saves"] = rg.backup_and_clear(ts, keep=not wipe)
        log(f"saves: backup {meta['saves'].get('backup')}; "
            f"{'removed ' + str(meta['saves'].get('removed')) if wipe else 'kept'}")
    ld = os.path.join(ROOT, "runs", "LAST_DAY.json")
    if os.path.exists(ld):   # one loop invocation: TOD reads the day from the screen, nothing is carried in
        os.remove(ld)
        log("removed runs/LAST_DAY.json")
    if not args.no_launch:
        log(f"launching {rg.STEAM_URL}")
        os.startfile(rg.STEAM_URL)
    try:
        hwnd = rg.wait_window(args.window_timeout)
    except TimeoutError as e:
        log(f"FAILED: {e}")
        return 1
    try:
        st = rg.wait_stable(hwnd, args.stable_timeout)
    except TimeoutError as e:   # an animated title screen is not fatal: the loop waits for stills itself
        log(f"WARNING: {e}; continuing")
        st = {"stable_after_s": None}
    hwnd = rg.unity_window_of({p.pid for p in rg.game_procs()}) or hwnd
    moved = io_win.ensure_onscreen(hwnd)
    if args.place:   # recording: put the client origin exactly here (e.g. 0,0) before the loop starts
        moved = place_window(hwnd, args.place)
    geo = rg.check_geometry(hwnd)
    meta.update(window_moved_to=moved, client=geo["client"], screen=geo["screen"], geometry_notes=geo["notes"])
    log(f"window stable after {st['stable_after_s']}s; client (x,y,w,h physical px) = {geo['client']} on a "
        f"{geo['screen'][0]}x{geo['screen'][1]} screen -- frame the recording on this rect")
    for n in geo["notes"]:
        log("NOTE: " + n)

    # ---- 3: gt (the stop condition depends on it) ----------------------------------------------------------------
    try:
        g = wait_gt(30)
    except ImportError as e:   # pymem missing: the loop could never stop at the Day 3 night
        log(f"FAILED: gt module unavailable ({e}); the Day {args.stop_day} night stop needs it")
        return 1
    if g.get("ok"):
        log(f"gt ok: screen={g.get('screen')} day={g.get('day')}")
    else:   # the title screen may not expose every pointer yet; the loop reads gt again every tick
        log(f"WARNING: gt not readable yet ({g.get('error')}); the loop retries it each tick")

    # ---- 4: remote extraction --------------------------------------------------------------------------------------
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + env.get("PYTHONPATH", "")
    if args.local_extract:
        env.pop("TOD_EXTRACT_URL", None)
        log("local extraction (no TOD_EXTRACT_URL)")
    else:
        ex = ensure_extractor(ts, args.tunnel_wait, args.extract_url)
        if not ex:
            log("FAILED: no remote extractor reachable (A100 8766, L4 8765; use --local-extract to run with "
                "local extraction)")
            return 1
        log(f"using the {ex[0]} extractor at {ex[1]}")
        meta["extractor"] = {"name": ex[0], "url": ex[1]}
        env["TOD_EXTRACT_URL"] = ex[1]

    # ---- 4b: --fast: TOD scoring on the tod-fast box instead of the public API -----------------------------------
    if args.fast:
        if not ensure_tunnel(FAST_URL, ts, max(args.tunnel_wait, 60), ["tunnel"], "fast", sh="deploy/gcp_fast.sh"):
            log("FAILED: tod-fast scorer not reachable (`deploy/gcp_fast.sh status`; start it with "
                "`deploy/gcp_fast.sh start`)")
            return 1
        h = health(FAST_URL) or {}
        log(f"TOD scoring on the fast box: TOD_API_URL={FAST_URL} (model {(h.get('picker') or {}).get('model')})")
        meta["tod_api_url"] = env["TOD_API_URL"] = FAST_URL

    # ---- 5: one loop invocation ---------------------------------------------------------------------------------
    cmd = [sys.executable, "-m", "tod_papers.loop", "--max-ticks", str(args.max_ticks), "--save-raw",
           "--stop-on-gt-day-end", str(args.stop_day)] + STOP_FLAGS
    if args.from_day is None:   # saves are kept: TOD is told to start a NEW story from the menu
        cmd.append("--new-story")
    if args.pause_think:
        cmd.append("--pause-think")
    cmd += args.loop_args.split()
    assert "--stop-on-screen" not in cmd, "--stop-on-screen day_end would stop at Day 1's night"
    meta["loop_cmd"] = cmd[1:]
    log("loop: " + " ".join(cmd[1:]))
    t0 = time.time()
    run_dir = None
    p = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", bufsize=1)
    try:
        for line in p.stdout:
            sys.stdout.write(line)
            if line.startswith("[loop] logging to "):
                run_dir = line.split("logging to ", 1)[1].strip()
        rc = p.wait()
    except KeyboardInterrupt:   # under --pause-think, prefer `type nul > runs\STOP_LOOP` (the game is resumed)
        p.terminate()
        rc = p.wait()
    except BaseException:       # never leave the loop playing headless (e.g. a stdout encoding error here)
        p.terminate()
        p.wait()
        raise
    meta.update(loop_rc=rc, run_dir=run_dir, loop_wall_s=round(time.time() - t0, 1))
    stop = None
    if run_dir and os.path.exists(os.path.join(run_dir, "summary.md")):
        stop = report.summary_md(run_dir)["stop"]
    meta["stop_reason"] = stop
    with open(os.path.join(ROOT, "runs", f"demo_{ts}.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1, default=str)
    log(f"loop rc={rc} stop={stop} wall={meta['loop_wall_s'] / 60:.1f} min run={run_dir}")

    # ---- 6: report -----------------------------------------------------------------------------------------------
    if run_dir:
        print(report.report(run_dir))
        if args.from_day:
            days = [t["gt"]["day"] for t in report.load(run_dir) if (t.get("gt") or {}).get("screen") == "DayScreen"]
            if days and days[0] != args.from_day:
                log(f"WARNING: --from-day {args.from_day} but the first booth day was {days[0]} (TOD picks the "
                    f"highest day tile; the saves' highest day was not {args.from_day})")
    return 0 if (stop or "").startswith("gt: day") else 4


if __name__ == "__main__":
    sys.exit(main())
