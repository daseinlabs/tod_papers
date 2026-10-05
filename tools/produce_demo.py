r"""produce_demo.py -- one command: a verified one-take demo recording of TOD playing Days 1..N (OBS Studio).

    .venv-loop\Scripts\python.exe tools\produce_demo.py --days 1      # Day 1 (the demo take)
    .venv-loop\Scripts\python.exe tools\produce_demo.py --days 3      # Days 1-3
    .venv-loop\Scripts\python.exe tools\produce_demo.py --rehearse    # no TOD credits: replay runs/20261004_115735
    .venv-loop\Scripts\python.exe tools\produce_demo.py --verify runs\<ts>   # verification + report only

OBS composes and records (docs/recording.md): Game Capture of PapersPlease.exe, WGC Window Capture of the
"TOD viewer" sidebar, an Image source for the tally PNG, Application Audio of the game only. Every capture is
occlusion-independent, so the user can keep working on the PC; only minimizing a captured window stalls it (the
director's watchdog restores it without activating it). Steps (logged to runs/produce_<ts>.log):
  0. preflight: A100 tunnel 127.0.0.1:8766 /health (tunnel started if down; instances are never started/stopped),
     ffmpeg/ffprobe, stale viewer / tally / director / loop processes of this repo killed;
  1. tools/obs_setup.py (idempotent: install, config, launch OBS in the tray, verify);
  2. viewer.py --follow --layout video --obs; tally.py --follow --final-day N (writes tally/tally_current.png);
  3. game: relaunched through Steam (demo_run's path; saves kept), client origin placed at 0,0; the `game` source
     must be live and non-black, else the WGC twin `game_wgc` is switched on and re-checked;
  4. obs_director.py --follow --final-day N: pre-take checks, StartRecord, live scene switching;
  5. demo_run.py --no-launch --place 0,0 --stop-day N (TOD starts a NEW story from the menu, no save wipe;
     --fast when the tod-fast scorer already answers on 8790; TOD_GRAB=wgc so a covered game is still seen);
  6. on loop exit (any reason): the director holds the final tally, then StopRecord (stop file if it does not);
  7. verify: ffprobe (3840x2160, 30 fps, audio, duration ~ recording), volumedetect, one <=640 px frame per scene
     type at the scene_log times -> runs/<ts>/verify/; the MP4 is moved to runs/<ts>/demo_day{N}.mp4;
     report.py -> runs/<ts>/report.md (+ a Recording section).
Exit: 0 verified, 2 verification failed, 3 take failed, 1 preflight failed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
RUNS = os.path.join(ROOT, "runs")
PY = sys.executable
sys.path.insert(0, HERE)

import psutil  # noqa: E402

A100_URL = "http://127.0.0.1:8766"
FAST_URL = "http://127.0.0.1:8790"
REHEARSAL_REF = os.path.join(RUNS, "20261004_115735")
OURS = ("tod_papers.loop", "demo_run.py", "viewer.py", "tally.py", "obs_director.py", "stage.py")
LOGF = None
os.environ["PYTHONIOENCODING"] = "utf-8"   # children write to log files
os.environ["PYTHONUTF8"] = "1"


def log(msg: str) -> None:
    line = f"[produce {time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if LOGF:
        LOGF.write(line + "\n")
        LOGF.flush()


def health(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url + "/health", timeout=5) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def kill_ours() -> list[str]:
    me = {os.getpid()} | {p.pid for p in psutil.Process().parents()}
    killed = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cl = " ".join(p.info["cmdline"] or [])
            if p.info["pid"] not in me and "python" in (p.info["name"] or "").lower() and ROOT.lower() in \
                    cl.lower() + " " + (p.cwd() or "").lower() and any(k in cl for k in OURS):
                killed.append(f"{p.pid} {cl[-70:]}")
                p.kill()
        except Exception:
            pass
    return killed


def spawn(args: list[str], name: str, ts: str) -> subprocess.Popen:
    path = os.path.join(RUNS, f"produce_{ts}_{name}.log")
    fh = open(path, "w", encoding="utf-8")
    log(f"$ {' '.join(args)}  (log runs/{os.path.basename(path)})")
    return subprocess.Popen([PY, "-u"] + args, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT)


def wait_for(pred, timeout: float, step: float = 0.5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


# ---- 0 --------------------------------------------------------------------------------------------------------------
def preflight(rehearse: bool) -> None:
    for t in ("ffmpeg", "ffprobe"):
        if not shutil.which(t):
            raise SystemExit(f"{t} not on PATH")
    log(f"stale repo processes killed: {kill_ours() or 'none'}")
    if rehearse:
        return
    h = health(A100_URL)
    if not h:
        import demo_run
        log("no /health on 8766 -> starting the A100 tunnel (deploy/gcp_l4_spot.sh tunnel --a100)")
        if not demo_run.ensure_tunnel(A100_URL, time.strftime("%Y%m%d_%H%M%S"), 120, ["tunnel", "--a100"], "A100"):
            raise SystemExit("A100 tunnel /health never answered (is the A100 instance running?)")
        h = health(A100_URL)
    log(f"A100 /health OK: warm={h.get('warm')} gpu={h.get('gpu')}")


# ---- 3: game --------------------------------------------------------------------------------------------------------
def launch_game(ts: str, timeout: float = 120) -> int:
    """demo_run's launch path: kill, back up the saves (kept), Steam relaunch, wait for a still frame, place 0,0."""
    import demo_run
    import reset_game as rg
    from tod_papers import io_win
    log(f"terminated {rg.kill_game() or 'nothing (game not running)'}")
    time.sleep(2.0)
    sv = rg.backup_and_clear(ts, keep=True)
    log(f"saves: backup {sv.get('backup')}; kept")
    os.startfile(rg.STEAM_URL)
    hwnd = rg.wait_window(timeout)
    try:
        st = rg.wait_stable(hwnd, 90)
        log(f"game window stable after {st['stable_after_s']} s"
            + (f" (animated {st['animated_title']} accepted)" if st.get("animated_title") else ""))
    except TimeoutError as e:
        log(f"WARNING: {e}; continuing")
    hwnd = rg.unity_window_of({p.pid for p in rg.game_procs()}) or hwnd
    io_win.ensure_onscreen(hwnd)
    demo_run.place_window(hwnd, "0,0")
    return hwnd


# ---- 7: verify ------------------------------------------------------------------------------------------------------
def ffprobe(path: str) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                       capture_output=True, text=True)
    return json.loads(r.stdout or "{}")


def volumedetect(path: str) -> dict:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-map", "0:a:0", "-af", "volumedetect",
                        "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = {}
    for k in ("mean_volume", "max_volume"):
        m = re.search(k + r": (-?[\d.]+|-inf) dB", r.stderr or "")
        out[k] = float(m.group(1)) if m and m.group(1) != "-inf" else None
    return out


def scene_frames(mp4: str, events: list[dict], dur: float, out_dir: str) -> list[dict]:
    """One <=640 px frame per scene type: the longest span of each, at its middle (after the 700 ms Move)."""
    spans = {}
    for i, e in enumerate(events):
        if e.get("rec_t") is None:
            continue
        end = next((x["rec_t"] for x in events[i + 1:] if x.get("rec_t") is not None), dur)
        if end - e["rec_t"] > spans.get(e["scene"], (0, 0, None))[1] - spans.get(e["scene"], (0, 0, None))[0]:
            spans[e["scene"]] = (e["rec_t"], end, e["reason"])
    os.makedirs(out_dir, exist_ok=True)
    frames = []
    for scene in ("GAME", "TWOUP", "TOD1", "TOD2", "TOD3", "TALLY"):
        if scene not in spans:
            continue
        a, b, why = spans[scene]
        t = min(max(a + 1.2, (a + b) / 2), max(a, dur - 0.5))
        png = os.path.join(out_dir, f"{scene}_{t:07.1f}s.png")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}", "-i", mp4,
                        "-frames:v", "1", "-vf", "scale=640:-2", png])
        frames.append({"scene": scene, "t": round(t, 2), "span": [round(a, 2), round(b, 2)], "reason": why,
                       "png": png if os.path.isfile(png) else None})
        log(f"  frame {scene:<5} @ {t:6.1f}s ({why[:60]}) -> {os.path.relpath(png, ROOT)}")
    return frames


def verify(run_dir: str, name: str | None = None) -> int:
    sl_path = os.path.join(run_dir, "scene_log.json")
    try:
        with open(sl_path, encoding="utf-8") as fh:
            sl = json.load(fh)
    except Exception as e:
        log(f"verify: no scene log ({e!r})")
        return 2
    src = sl.get("output_path") or ""
    mp4 = os.path.join(run_dir, name) if name else None
    if src and os.path.isfile(src) and mp4 and os.path.normcase(os.path.abspath(src)) != os.path.normcase(mp4):
        for i in range(60):   # OBS keeps the file open for a few s while it finalizes the Hybrid MP4
            try:
                os.replace(src, mp4)
                break
            except PermissionError:
                time.sleep(1.0)
        else:
            shutil.copyfile(src, mp4)
            log(f"recording still locked by OBS after 60 s: copied (original stays in {src})")
        sl["output_path_moved_to"] = mp4
        with open(sl_path, "w", encoding="utf-8") as fh:
            json.dump(sl, fh, indent=1)
        log(f"recording -> {os.path.relpath(mp4, ROOT)}")
    elif not mp4 or not os.path.isfile(mp4):
        mp4 = sl.get("output_path_moved_to") or src
    if not mp4 or not os.path.isfile(mp4):
        log(f"verify: recording not found ({src})")
        return 2
    pr = ffprobe(mp4)
    v = next((s for s in pr.get("streams", []) if s.get("codec_type") == "video"), {})
    au = next((s for s in pr.get("streams", []) if s.get("codec_type") == "audio"), None)
    dur = float(pr.get("format", {}).get("duration", 0))
    ev = sl.get("events") or []
    rec_len = (sl["record_stop"] - sl["record_start"]) if sl.get("record_start") and sl.get("record_stop") else None
    vol = volumedetect(mp4) if au else {}
    checks = {
        "resolution 3840x2160": (v.get("width"), v.get("height")) == (3840, 2160),
        "fps 30": v.get("r_frame_rate") in ("30/1", "30000/1000"),
        "audio stream": au is not None,
        "game audio not silent (max > -50 dB)": (vol["max_volume"] if vol.get("max_volume") is not None else -99) > -50,   # 0.0 dB is loud
        "duration ~ recording (+-5 s)": rec_len is None or abs(dur - rec_len) <= 5.0,
        "scene log has GAME, TOD and TALLY": {"GAME", "TALLY"} <= {e["scene"] for e in ev}
        and bool({"TOD1", "TOD2"} & {e["scene"] for e in ev}),
    }
    info = {"mp4": mp4, "codec": v.get("codec_name"), "profile": v.get("profile"), "width": v.get("width"),
            "height": v.get("height"), "fps": v.get("r_frame_rate"), "duration_s": round(dur, 2),
            "recording_s": round(rec_len, 2) if rec_len else None,
            "audio": f"{au.get('codec_name')} {au.get('sample_rate')} Hz {au.get('channels')} ch" if au else None,
            "volume": vol, "size_mb": round(os.path.getsize(mp4) / 1e6, 1), "checks": checks,
            "scenes": len(ev)}
    log("ffprobe: " + json.dumps({k: x for k, x in info.items() if k != "checks"}))
    for k, ok in checks.items():
        log(f"  {'OK  ' if ok else 'FAIL'} {k}")
    info["frames"] = scene_frames(mp4, ev, dur, os.path.join(run_dir, "verify"))
    with open(os.path.join(run_dir, "verification.json"), "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=1)
    rep = ""
    try:
        r = subprocess.run([PY, "tools/report.py", run_dir], cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        rep = r.stdout or ""
    except Exception as e:
        log(f"report.py failed: {e!r}")
    rows = "\n".join(f"| {e.get('rec_t')} | {e['scene']} | {e['reason']} |" for e in ev)
    rep += (f"\n\n## Recording\n\n`{os.path.relpath(mp4, ROOT)}`: {info['codec']} {info['width']}x{info['height']} "
            f"@ {info['fps']}, {info['duration_s']} s, {info['size_mb']} MB, audio {info['audio']} "
            f"(mean {vol.get('mean_volume')} dB, max {vol.get('max_volume')} dB). Checks: "
            + ", ".join(f"{k} {'OK' if ok else 'FAIL'}" for k, ok in checks.items())
            + f"\n\n| rec s | scene | reason |\n|---|---|---|\n{rows}\n\nFrames: runs/<ts>/verify/.\n")
    with open(os.path.join(run_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(rep)
    log(f"report -> {os.path.relpath(os.path.join(run_dir, 'report.md'), ROOT)}")
    return 0 if all(checks.values()) else 2


# ---- 1-6: the take --------------------------------------------------------------------------------------------------
def take(a, ts: str) -> dict:
    import obs_director
    import obs_setup
    res = {"ok": False, "run_dir": None}
    procs = []
    try:
        cl = obs_setup.setup()
        st = cl.get_record_status()
        if st.output_active:
            log("OBS was recording: stopping that recording first")
            cl.stop_record()
        procs.append(spawn(["tools/viewer.py", "--follow", "--layout", "video", "--obs"], "viewer", ts))
        final_day = 3 if a.rehearse else a.days   # the reference run is a Day 3 run
        procs.append(spawn(["tools/tally.py", "--follow", "--final-day", str(final_day)], "tally", ts))
        import win32gui
        if not wait_for(lambda: win32gui.FindWindow(None, "TOD viewer"), 30):
            res["why"] = "the TOD viewer window never appeared"
            return res
        time.sleep(1.0)
        m = obs_director.keep_on_screen("TOD viewer")   # Tk does not paint off-screen parts (WGC would get white)
        if m:
            log("moved " + m)
        if not a.rehearse:
            launch_game(ts)
        src, why = obs_director.ensure_game(cl, wait_s=20)
        log(f"game source: {src or 'BLACK on Game Capture and WGC'} ({why})")
        if not src:
            res["why"] = "game capture black"
            return res
        dargs = ["tools/obs_director.py", "--final-day", str(final_day), "--final-hold", str(a.final_hold)]
        if a.rehearse:   # dwell times scale by 2/speed inside the director
            dargs += ["--replay", a.rehearse_ref, "--speed", str(a.speed), "--from-tick", str(a.from_tick)] \
                + (["--skip", a.skip] if a.skip else [])
        else:   # live: StartRecord when the loop's run dir appears (~3 s before tick 0), not during demo_run startup
            dargs += ["--follow", "--record-on-run"]
        director = spawn(dargs, "director", ts)
        procs.append(director)
        dlog_path = os.path.join(RUNS, f"produce_{ts}_director.log")

        def _dlog_has(s_):
            try:
                return s_ in open(dlog_path, encoding="utf-8").read()
            except OSError:
                return False
        ready = (lambda: _dlog_has("recording (")) if a.rehearse else (lambda: _dlog_has("armed: recording starts"))
        if not wait_for(lambda: director.poll() is not None or ready(), 90):
            res["why"] = "director never became ready (recording / armed)"
            return res
        if director.poll() is not None:
            res["why"] = f"director exited rc={director.returncode} (pre-take check failed? see its log)"
            return res
        log("recording" if a.rehearse else "director armed: recording starts with the loop's run dir")
        if a.rehearse:
            rc = 0
        else:
            cmd = [PY, "tools/demo_run.py", "--no-launch", "--place", "0,0", "--stop-day", str(a.days)]
            if a.pause_think:
                cmd.append("--pause-think")
            if health(FAST_URL):
                cmd.append("--fast")
                log("tod-fast scorer answers on 8790 -> --fast")
            env = dict(os.environ, TOD_GRAB="wgc")
            dlog = os.path.join(RUNS, f"produce_{ts}_demo_run.log")
            log(f"$ TOD_GRAB=wgc {' '.join(cmd[1:])}  (log runs/{os.path.basename(dlog)})")
            with open(dlog, "w", encoding="utf-8") as fh:
                rc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT).returncode
            log(f"demo_run rc={rc}")
        # 6: the director holds the final tally and stops the recording itself; nudge it if it does not
        if director.poll() is None and not wait_for(lambda: director.poll() is not None,
                                                    (300 if a.rehearse else 0) + a.final_hold + 45, 1.0):
            log("director still running -> stop file")
            subprocess.run([PY, "tools/obs_director.py", "--stop"], cwd=ROOT)
            if not wait_for(lambda: director.poll() is not None, a.final_hold + 30, 1.0):
                director.kill()
                cl = obs_setup.connect()
                if cl and cl.get_record_status().output_active:
                    cl.stop_record()
        log(f"director rc={director.returncode}")
        dlog = open(os.path.join(RUNS, f"produce_{ts}_director.log"), encoding="utf-8").read()
        m = re.findall(r"following (\S+)", dlog)
        res["run_dir"] = m[-1] if m else None
        res["ok"] = rc == 0 and director.returncode == 0 and bool(res["run_dir"])
        res["why"] = f"demo_run rc={rc}, director rc={director.returncode}"
        return res
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()


def main(argv=None) -> int:
    global LOGF
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=1, help="stop at this day's night (1..3)")
    ap.add_argument("--pause-think", action="store_true", help="pass --pause-think to demo_run")
    ap.add_argument("--final-hold", type=float, default=12.0, help="s the final tally stays up before StopRecord")
    ap.add_argument("--verify", metavar="RUN_DIR", help="only step 7 on a finished take")
    ap.add_argument("--name", help="output mp4 name (default demo_day{N}.mp4)")
    ap.add_argument("--rehearse", action="store_true",
                    help="no TOD credits: the game stays as it is, the director replays --rehearse-ref")
    ap.add_argument("--rehearse-ref", default=REHEARSAL_REF)
    ap.add_argument("--speed", type=float, default=4.0)
    ap.add_argument("--from-tick", type=int, default=148)
    ap.add_argument("--skip", default="177-244")
    a = ap.parse_args(argv)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(RUNS, exist_ok=True)
    LOGF = open(os.path.join(RUNS, f"produce_{ts}.log"), "a", encoding="utf-8")
    name = a.name or ("rehearsal.mp4" if a.rehearse else f"demo_day{a.days}.mp4")
    if a.verify:
        return verify(os.path.abspath(a.verify), name)
    try:
        preflight(a.rehearse)
    except SystemExit as e:
        log(f"preflight FAILED: {e}")
        return 1
    res = take(a, ts)
    log(f"take: {json.dumps(res)}")
    if not res.get("run_dir"):
        return 3
    rc = verify(res["run_dir"], name)
    return rc if res["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
