r"""obs_director.py -- live OBS scene director for the one-take demo (docs/recording.md).

    .venv-loop\Scripts\python.exe tools\obs_director.py --follow --final-day 1          # the take (produce_demo)
    .venv-loop\Scripts\python.exe tools\obs_director.py --check                         # pre-take checks only
    .venv-loop\Scripts\python.exe tools\obs_director.py --replay runs\20261004_115735 --speed 4 --from-tick 148 --skip 177-244
    .venv-loop\Scripts\python.exe tools\obs_director.py --stop                          # ask a running director to end

Read-only on the run: tails runs/<ts>/tick_NNNN.json like tools/viewer.py --follow (tod_papers.viewer reader) and
switches OBS program scenes over obs-websocket v5 (obsws-python) with the Move transition (700 ms). It never talks to
the loop or the game; if the websocket drops it reconnects and re-applies the current scene, and any error inside a
poll is logged, never fatal (a dead director freezes the view, not the recording).

Rules (the tools/cap_zoom_plan.py cycle, applied online; dwell times x --dwell-scale):
  * before the booth (title, menus, news, day intro) -> GAME
  * booth entrants (gt.day_processed while gt.screen == DayScreen; entrant k = day_processed): k mod 4 = 0,1 -> GAME,
    2 -> TWOUP, 3 -> TOD1 for --tod-t1 s then TOD2 (pan to the second tile) until the entrant ends; a GAME entrant
    with no stamp press after --slow-twoup-ticks (12) ticks -> TWOUP for the rest of it
  * no-documents / Jorji entrant (state.no_documents_presented = yes on >= --nodocs-min-run consecutive ticks of one
    entrant): forced TWOUP for --nodocs-twoup s, then TOD1 -> TOD2 as above
  * min span: a switch is held back until the current scene has been up --min-span s (the latest wish wins), so a
    fast entrant folds into the previous view; day-end and tally cues are exempt
  * day end (gt NightScreen of day d, or day d -> d+1; tod_papers.tally.day_end_events, the same cue tally.py fires
    on) -> TWOUP; TALLY once runs/<ts>/tally/tally_current.png is up (> 1x1 px; tally.py --follow writes it) or after
    --tally-wait s; back to GAME when the PNG goes blank again (or --tally-secs) -> next day's entrants
  * final (day --final-day ends, the run's summary.md appears, or --stop / the stop file): TALLY, hold --final-hold s,
    StopRecord, exit
Every switch is appended to runs/<ts>/scene_log.json (wall time, time into the recording, scene, reason, tick) and
sent as a Hybrid-MP4 chapter. A watchdog (5 s) un-minimizes the game / viewer windows without activating them and
puts them at the bottom of the z-order (WGC and Game Capture do not need them visible, only not minimized).

Pre-take checks (--check, and before StartRecord): websocket + video settings; `game` (Game Capture) live and
non-black via GetSourceScreenshot (else the WGC twin `game_wgc` is enabled in every scene and re-checked); `sidebar`
live and non-black; disk space; NVENC (OBS log lists obs_nvenc_h264_tex).
--replay REF: rehearsal without TOD: copies REF's ticks into a fresh runs/<now>_rehearsal dir at their own spacing /
--speed (ticks before --from-tick at once), so viewer.py --follow, tally.py --follow and this director all follow it
exactly as live. Exit: 0 recorded and stopped, 1 pre-take check failed, 2 OBS unreachable.
"""
from __future__ import annotations

import argparse
import base64
import glob
import io
import json
import logging
import os
import shutil
import struct
import sys
import threading
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
RUNS = os.path.join(ROOT, "runs")
STOP_FILE = os.path.join(RUNS, "obs", "director.stop")
sys.path[:0] = [HERE, os.path.join(ROOT, "src")]

import obs_setup as S  # noqa: E402
from tod_papers.viewer import RunReader, list_ticks, newest_run  # noqa: E402
from tod_papers import tally as TL  # noqa: E402

logging.getLogger("obsws_python").setLevel(logging.CRITICAL)   # its tracebacks on every failed request
DAY = "DayScreen"
CYCLE = ("GAME", "GAME", "TWOUP", "TOD")   # --in 2, --twoup 1, --tod 1


def log(msg: str) -> None:
    print(f"[director {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- tick fields (as cap_zoom_plan.load_ticks)
def row(r: dict) -> dict:
    g = r.get("gt") or {}
    st = r.get("state") or {}
    v = st.get("no_documents_presented")
    v = v.get("value") if isinstance(v, dict) else v
    return {"tick": r.get("tick"), "screen": g.get("screen") if g.get("ok", True) else None, "day": g.get("day"),
            "proc": g.get("day_processed"), "nd": v in (True, "yes"),
            "stamped": bool((r.get("entrant") or {}).get("stamp_clicks"))}


def png_up(path: str) -> bool:
    """tally_current.png is a 1x1 transparent PNG while the tally is down."""
    try:
        with open(path, "rb") as fh:
            h = fh.read(24)
        w, ht = struct.unpack(">II", h[16:24])
        return w > 1 and ht > 1
    except Exception:
        return False


# ---------------------------------------------------------------- OBS connection (reconnecting)
class OBS:
    def __init__(self):
        self.cl = None
        self.next_try = 0.0
        self.scene = None   # what we last asked for (re-applied after a reconnect)

    def get(self):
        if self.cl is not None:
            return self.cl
        if time.time() < self.next_try:
            return None
        cl = S.connect(timeout=3)
        if cl and S.ready(cl):
            self.cl = cl
            try:
                tl = [t["transitionName"] for t in cl.get_scene_transition_list().transitions]
                cl.set_current_scene_transition(S.TRANSITION if S.TRANSITION in tl else "Fade")
                cl.set_current_scene_transition_duration(S.TRANSITION_MS)
            except Exception as e:
                log(f"transition setup: {e!r}")
            log("websocket connected")
            if self.scene:
                self.call("set_current_program_scene", self.scene)
            return cl
        self.next_try = time.time() + 3.0
        return None

    def call(self, fn: str, *args):
        cl = self.get()
        if cl is None:
            return None
        try:
            return getattr(cl, fn)(*args)
        except Exception as e:
            log(f"websocket {fn} failed ({type(e).__name__}: {str(e)[:120]}); reconnecting")
            try:
                cl.disconnect()
            except Exception:
                pass
            self.cl, self.next_try = None, time.time() + 1.0
            return None

    def chapter(self, name: str) -> None:
        cl = self.get()
        if cl is not None:
            try:
                cl.send("CreateRecordChapter", {"chapterName": name})
            except Exception:
                pass


# ---------------------------------------------------------------- pre-take checks
def screenshot(cl, source: str, w: int = 320, h: int = 180):
    from PIL import Image
    r = cl.get_source_screenshot(source, "png", w, h, -1)
    return Image.open(io.BytesIO(base64.b64decode(r.image_data.split(",", 1)[1]))).convert("RGB")


def nonblack(cl, source: str) -> tuple[bool, str]:
    from PIL import ImageStat
    try:
        if not cl.get_source_active(source).video_active:
            return False, "inactive"
        st = ImageStat.Stat(screenshot(cl, source))
        mean, sd = sum(st.mean) / 3, max(st.stddev)
        return (mean > 6 or sd > 6), f"mean {mean:.1f} sd {sd:.1f}"
    except Exception as e:
        return False, f"error {str(e)[:80]}"


def set_game_source(cl, use_wgc: bool) -> None:
    for sname in S.SCENES:
        for it in cl.get_scene_item_list(sname).scene_items:
            if it["sourceName"] in ("game", "game_wgc"):
                cl.set_scene_item_enabled(sname, it["sceneItemId"], (it["sourceName"] == "game_wgc") == use_wgc)


def ensure_game(cl, wait_s: float = 15.0) -> tuple[str | None, str]:
    """Which game source is live and non-black: 'game' (Game Capture), else 'game_wgc' (switched on), else None."""
    for src, wgc in (("game", False), ("game_wgc", True)):
        set_game_source(cl, wgc)
        t0, why = time.time(), ""
        while time.time() - t0 < wait_s:
            ok, why = nonblack(cl, src)
            if ok:
                return src, why
            time.sleep(1.0)
        log(f"{src}: black / not live ({why})" + ("; switching to the WGC window capture" if not wgc else ""))
    set_game_source(cl, False)
    return None, why


def nvenc_ok() -> bool:
    logs = sorted(glob.glob(os.path.join(S.CFG, "logs", "*.txt")), key=os.path.getmtime)
    if not logs:
        return False
    with open(logs[-1], encoding="utf-8", errors="replace") as fh:
        return "obs_nvenc_h264_tex" in fh.read()


def precheck(cl, min_free_gb: float, need_sidebar: bool = True, sidebar_wait: float = 30.0) -> list[str]:
    S.verify(cl, fix=True)
    probs = [p for p in S.verify(cl, fix=False) if "not loaded" not in p]
    src, why = ensure_game(cl)
    log(f"game source: {src or 'NONE'} ({why})")
    if not src:
        probs.append(f"game capture black on both methods ({why})")
    if need_sidebar:
        m = keep_on_screen("TOD viewer")
        if m:
            log("moved " + m)
            time.sleep(1.0)
        t0 = time.time()
        while True:
            ok, why = nonblack(cl, "sidebar")
            if ok or time.time() - t0 > sidebar_wait:
                break
            time.sleep(1.0)
        log(f"sidebar: {'ok' if ok else 'NOT LIVE'} ({why})")
        if not ok:
            probs.append(f"sidebar window capture not live ({why}); is `tools/viewer.py --obs` running?")
    free = shutil.disk_usage(S.RUNS_OBS).free / 1e9
    log(f"disk: {free:.0f} GB free on the runs/obs drive (~0.4 GB/min at 4K30 CQP 18)")
    if free < min_free_gb:
        probs.append(f"only {free:.1f} GB free (< {min_free_gb})")
    if not nvenc_ok():
        probs.append("NVENC encoder not listed in the OBS log")
    return probs


# ---------------------------------------------------------------- rehearsal feeder
def copy_tick(ref: str, dst: str, t: int) -> None:
    png = f"tick_{t:04d}.png" if os.path.exists(os.path.join(ref, f"tick_{t:04d}.png")) else f"raw_{t:04d}.png"
    for name in (png, f"tick_{t:04d}.json"):   # what viewer.RunReader.som / rec read
        s = os.path.join(ref, name)
        if os.path.exists(s):
            shutil.copyfile(s, os.path.join(dst, name + ".part"))
            os.replace(os.path.join(dst, name + ".part"), os.path.join(dst, name))


def feeder(ref: str, dst: str, speed: float, from_tick: int, end_tick: int | None, done: threading.Event,
           skip: tuple | None = None) -> None:
    """Copy REF's ticks into dst at their recorded spacing / speed (the loop's write order: png, then json).
    Ticks before from_tick and inside skip=(a, b) are copied at once (the clock re-bases after a skip)."""
    ticks = [t for t in list_ticks(ref) if end_tick is None or t <= end_tick]
    times = {}
    for t in ticks:
        with open(os.path.join(ref, f"tick_{t:04d}.json"), encoding="utf-8") as fh:
            times[t] = json.load(fh).get("time")
    base_t, w0 = None, time.time()
    for t in ticks:
        if t < from_tick:
            continue   # history: copied by main() before the take starts
        instant = bool(skip and skip[0] <= t <= skip[1])
        if not instant and times[t]:
            if base_t is None:
                base_t, w0 = times[t], time.time()
            while time.time() - w0 < (times[t] - base_t) / speed:
                time.sleep(0.05)
        elif instant:
            base_t = None   # re-base on the next paced tick
            time.sleep(0.02)
        copy_tick(ref, dst, t)
    if os.path.exists(os.path.join(ref, "summary.md")) and end_tick is None:
        shutil.copyfile(os.path.join(ref, "summary.md"), os.path.join(dst, "summary.md"))
    log(f"replay feeder done ({len(ticks)} ticks)")
    done.set()


# ---------------------------------------------------------------- the director
class Director:
    def __init__(self, a, obs: OBS):
        self.a, self.obs = a, obs
        sc = a.dwell_scale
        self.min_span, self.t1, self.nd_twoup = a.min_span * sc, a.tod_t1 * sc, a.nodocs_twoup * sc
        self.tally_secs, self.tally_wait, self.final_hold = a.tally_secs * sc, a.tally_wait * sc, a.final_hold * sc
        self.run = None
        self.silent = False
        self.reader = None
        self.seen = -1
        self.raw: list[dict] = []
        self.fired: set = set()
        self.cur = None          # current program scene
        self.cur_since = 0.0
        self.want = ("GAME", "start")
        self.ent = None          # (day, proc) of the current booth entrant
        self.ent_t0 = 0.0
        self.ent_mode = "GAME"
        self.nd_run = 0
        self.nd_forced_t = None
        self.ent_ticks = 0       # booth ticks of the current entrant (slow entrant -> TWOUP)
        self.eod = None          # {"day", "final", "t0", "tally_t": time the tally went up or None}
        self.final_t = None
        self.rec_t0 = None
        self.log_rows: list[dict] = []
        self.output_path = None
        self.rec_stop = None

    # -- scene switching
    def switch(self, scene: str, why: str, force: bool = False) -> None:
        if self.silent:   # history ticks at attach: entrant state only, no scene wishes
            return
        now = time.time()
        if scene == self.cur:
            self.want = (scene, why)   # also cancels a wish that the min span is still holding back
            return
        if not force and self.cur is not None and now - self.cur_since < self.min_span:
            self.want = (scene, why)   # held back by the min span; applied in step()
            return
        r = self.obs.call("set_current_program_scene", scene)
        self.obs.scene = scene
        if r is None and self.obs.cl is None:
            log(f"switch {scene} ({why}) not sent (websocket down); will re-apply on reconnect")
        prev, self.cur, self.cur_since, self.want = self.cur, scene, now, (scene, why)
        rec = round(now - self.rec_t0, 2) if self.rec_t0 else None
        tick = self.raw[-1].get("tick") if self.raw else None
        self.log_rows.append({"t": round(now, 3), "rec_t": rec, "scene": scene, "from": prev, "reason": why,
                              "tick": tick})
        log(f"{scene:<5} <- {prev or '-':<5} rec {rec if rec is not None else '-':>7}s  tick {tick}  {why}")
        self.obs.chapter(f"{scene}: {why}"[:60])
        self.write_log()

    def write_log(self) -> None:
        if not self.run:
            return
        doc = {"run": self.run, "record_start": self.rec_t0, "record_stop": self.rec_stop,
               "output_path": self.output_path,
               "transition": f"{S.TRANSITION} {S.TRANSITION_MS} ms", "events": self.log_rows}
        p = os.path.join(self.run, "scene_log.json")
        try:
            with open(p + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=1)
            os.replace(p + ".tmp", p)
        except OSError as e:
            log(f"scene_log write failed: {e!r}")

    # -- run attach / ticks
    def attach(self, run_dir: str) -> None:
        self.run, self.reader, self.seen, self.raw = run_dir, RunReader(run_dir), -1, []
        self.fired = set()
        tp = os.path.join(run_dir, "tally", "tally_current.png")
        self.tally_png = tp
        if self.obs.call("set_input_settings", "tally", {"file": tp}, True) is not None or self.obs.cl:
            log(f"tally image source -> {os.path.relpath(tp, ROOT)}")
        # the ticks already on disk are history: entrant / day-end state only, no scene wishes
        self.silent = True
        n = 0
        for r in self.poll_ticks():
            self.on_tick(r)
            n += 1
        self.silent = False
        log(f"following {run_dir} ({n} history ticks, state only)")
        self.write_log()

    def poll_ticks(self) -> list[dict]:
        new = []
        for t in list_ticks(self.run):
            if t <= self.seen:
                continue
            r = self.reader.rec(t)
            if r is None:   # still being written
                break
            self.seen = t
            self.raw.append(r)
            new.append(r)
        return new

    def on_tick(self, r: dict) -> None:
        now = time.time()
        x = row(r)
        # day end / final (the same events tally.py pops on)
        for e in TL.day_end_events(self.raw, self.a.final_day):
            k = (e["kind"], e["day"])
            if k in self.fired:
                continue
            self.fired.add(k)
            if self.eod is None and self.final_t is None and not self.silent:
                self.eod = {"day": e["day"], "final": e["kind"] == "final", "t0": now, "tally_t": None,
                            "why": e["why"]}
                self.switch("TWOUP", f"day {e['day']} end ({e['why']})", force=True)
        if self.eod is not None:
            return
        if x["screen"] != DAY or x["proc"] is None:
            if self.ent is not None and x["screen"] not in (DAY, None):
                self.ent = None
            if self.ent is None:
                self.switch("GAME", f"{x['screen'] or 'pre-booth'}")
            return
        key = (x["day"], x["proc"])
        if key != self.ent:   # a new entrant
            self.ent, self.ent_t0, self.nd_run, self.nd_forced_t, self.ent_ticks = key, now, 0, None, 0
            mode = CYCLE[x["proc"] % len(CYCLE)]
            self.ent_mode = mode
            scene = "TOD1" if mode == "TOD" else mode
            self.switch(scene, f"day {x['day']} entrant #{x['proc'] + 1}: {mode}")
        self.ent_ticks += 1
        if (self.ent_mode == "GAME" and not x["stamped"] and not self.silent
                and self.ent_ticks >= self.a.slow_twoup_ticks):
            # a slow entrant (no stamp after N ticks) is not minutes of GAME: TWOUP for the rest of it
            self.ent_mode = "SLOW"
            self.switch("TWOUP", f"day {x['day']} entrant #{x['proc'] + 1}: {self.ent_ticks} ticks, no stamp -> TWOUP")
        if x["nd"]:
            self.nd_run += 1
            if self.nd_run >= self.a.nodocs_min_run and self.nd_forced_t is None and self.ent_mode != "TOD":
                self.nd_forced_t, self.ent_mode = now, "NODOCS"
                self.switch("TWOUP", f"day {x['day']} entrant #{x['proc'] + 1}: no documents (Jorji) -> TWOUP")
        else:
            self.nd_run = 0

    # -- time-based transitions (called every poll)
    def step(self) -> bool:
        """False when the take is over."""
        now = time.time()
        if self.final_t is not None:
            if now - self.final_t >= self.final_hold:
                return False
            return True
        if self.eod is not None:
            e = self.eod
            up = png_up(self.tally_png)
            if e["tally_t"] is None:
                if up or now - e["t0"] >= self.tally_wait:
                    e["tally_t"] = now
                    self.switch("TALLY", f"day {e['day']} tally {'up' if up else '(no png; timed)'}", force=True)
                    if e["final"]:
                        self.final_t = now
            elif not e["final"]:
                if (not up and now - e["tally_t"] >= 2.0) or now - e["tally_t"] >= self.tally_secs + 3:
                    self.eod, self.ent = None, None
                    self.switch("GAME", f"day {e['day']} tally down -> next day", force=True)
            return True
        # pending wish held back by the min span
        if self.want[0] != self.cur and now - self.cur_since >= self.min_span:
            self.switch(*self.want)
        # TOD1 -> TOD2 pan; no-docs TWOUP -> TOD1
        if self.ent is not None:
            if self.ent_mode == "NODOCS" and self.cur == "TWOUP" and now - self.cur_since >= self.nd_twoup:
                self.ent_mode = "TOD"
                self.ent_t0 = now
                self.switch("TOD1", f"day {self.ent[0]} entrant #{self.ent[1] + 1}: no documents -> TOD", force=True)
            elif self.ent_mode == "TOD" and self.cur == "TOD1" and now - self.cur_since >= self.t1:
                self.switch("TOD2", f"day {self.ent[0]} entrant #{self.ent[1] + 1}: TOD tile 2", force=True)
        return True

    def finish(self, why: str) -> None:
        """Run ended without the final day-end cue (loop stopped / --stop): TALLY (tally.py pops on run_end), hold."""
        if self.final_t is not None:
            return
        t0 = time.time()
        while time.time() - t0 < self.tally_wait and not png_up(getattr(self, "tally_png", "")):
            time.sleep(0.25)
        self.eod = {"day": None, "final": True, "t0": t0, "tally_t": time.time(), "why": why}
        self.final_t = time.time()
        self.switch("TALLY", f"final: {why}", force=True)


def keep_on_screen(title: str = "TOD viewer") -> str | None:
    """Tk does not paint the part of a window that lies outside the desktop, so WGC captures it white: move the
    window fully onto its monitor (no activate, z-order unchanged). Occlusion is fine; off-screen is not."""
    import win32api
    import win32con
    import win32gui
    h = win32gui.FindWindow(None, title)
    if not h or win32gui.IsIconic(h):
        return None
    l, t, r, b = win32gui.GetWindowRect(h)
    ml, mt, mr, mb = win32api.GetMonitorInfo(win32api.MonitorFromWindow(h, 2))["Monitor"]
    nl = max(ml, min(l, mr - (r - l)))
    nt = max(mt, min(t, mb - (b - t)))
    if (nl, nt) == (l, t):
        return None
    win32gui.SetWindowPos(h, 0, nl, nt, 0, 0, win32con.SWP_NOSIZE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
    return f"'{title}' {l},{t} -> {nl},{nt} (was partly off-screen)"


def watchdog(names=("PapersPlease", "TOD viewer")) -> None:
    """Un-minimize the captured windows without activating them; park them at the bottom of the z-order; keep the
    viewer fully on-screen."""
    try:
        import win32con
        import win32gui
        for n in names:
            h = win32gui.FindWindow(None, n)
            if h and win32gui.IsIconic(h):
                win32gui.ShowWindow(h, win32con.SW_SHOWNOACTIVATE)
                win32gui.SetWindowPos(h, win32con.HWND_BOTTOM, 0, 0, 0, 0,
                                      win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
                log(f"watchdog: '{n}' was minimized -> restored (no activate, bottom of z-order)")
        m = keep_on_screen("TOD viewer")
        if m:
            log("watchdog: moved " + m)
    except Exception as e:
        log(f"watchdog: {e!r}")


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", help="follow this run dir")
    ap.add_argument("--follow", action="store_true", help="follow the newest run created after the director starts")
    ap.add_argument("--replay", metavar="REF", help="rehearsal: feed REF's ticks into runs/<now>_rehearsal and follow")
    ap.add_argument("--speed", type=float, default=4.0, help="replay speed")
    ap.add_argument("--from-tick", type=int, default=0, help="replay: ticks before this are copied at once")
    ap.add_argument("--end-tick", type=int, default=None, help="replay: last tick to feed")
    ap.add_argument("--skip", default=None, metavar="A-B",
                    help="replay: feed ticks A..B at once (time compression; every tick is still seen)")
    ap.add_argument("--check", action="store_true", help="pre-take checks only, then exit")
    ap.add_argument("--stop", action="store_true", help="tell a running director to finish (stop file)")
    ap.add_argument("--no-record", action="store_true", help="switch scenes only (no StartRecord/StopRecord)")
    ap.add_argument("--record-on-run", action="store_true",
                    help="--follow: StartRecord only when the loop's new run dir appears (~3 s before tick 0 is "
                         "logged), not at director start -- no title-screen dead air while demo_run starts up")
    ap.add_argument("--no-precheck", action="store_true")
    ap.add_argument("--final-day", type=int, default=3, help="the day whose end is the final tally")
    ap.add_argument("--min-span", type=float, default=15.0)
    ap.add_argument("--tod-t1", type=float, default=20.0, help="TOD1 hold before the pan to tile 2")
    ap.add_argument("--slow-twoup-ticks", type=int, default=12,
                    help="GAME entrant with no stamp press after this many ticks -> TWOUP")
    ap.add_argument("--nodocs-twoup", type=float, default=15.0, help="no-docs entrant: TWOUP before TOD")
    ap.add_argument("--nodocs-min-run", type=int, default=3)
    ap.add_argument("--tally-secs", type=float, default=12.0, help="a day-end tally's time up (tally.py --secs)")
    ap.add_argument("--tally-wait", type=float, default=6.0, help="max TWOUP lead before TALLY at a day end")
    ap.add_argument("--final-hold", type=float, default=12.0, help="final tally hold before StopRecord")
    ap.add_argument("--dwell-scale", type=float, default=None,
                    help="multiply every dwell above (default 1; --replay: 2/speed)")
    ap.add_argument("--min-free-gb", type=float, default=10.0)
    ap.add_argument("--max-minutes", type=float, default=240.0, help="safety stop")
    ap.add_argument("--poll", type=float, default=0.25)
    a = ap.parse_args(argv)
    a.skip = tuple(int(v) for v in a.skip.split("-")) if a.skip else None
    if a.dwell_scale is None:
        a.dwell_scale = 2.0 / a.speed if a.replay else 1.0
    if a.stop:
        os.makedirs(os.path.dirname(STOP_FILE), exist_ok=True)
        open(STOP_FILE, "w").close()
        log("stop file written")
        return 0
    obs = OBS()
    t0 = time.time()
    while obs.get() is None:
        if time.time() - t0 > 30:
            log("OBS websocket unreachable (run tools/obs_setup.py)")
            return 2
        time.sleep(1.0)
    if a.check or not a.no_precheck:
        probs = precheck(obs.cl, a.min_free_gb)
        for p in probs:
            log("CHECK FAILED: " + p)
        if a.check or probs:
            log("pre-take checks " + ("passed" if not probs else "FAILED"))
            return 1 if probs else 0
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)

    d = Director(a, obs)
    start_newest = newest_run(RUNS)
    feed_done = threading.Event()
    if a.replay:
        dst = os.path.join(RUNS, datetime.now().strftime("%Y%m%d_%H%M%S") + "_rehearsal")
        os.makedirs(dst)
        hist = [t for t in list_ticks(a.replay) if t < a.from_tick]
        for t in hist:
            copy_tick(a.replay, dst, t)
        log(f"rehearsal run {dst}: {len(hist)} history ticks copied; feeding from tick {a.from_tick} at {a.speed}x"
            + (f", ticks {a.skip[0]}-{a.skip[1]} at once" if a.skip else ""))
        d.attach(dst)
    elif a.run:
        d.attach(os.path.abspath(a.run))
    d.switch("GAME", "take start", force=True)
    def start_rec():
        st = obs.call("get_record_status")
        if st is not None and st.output_active:
            log("a recording was already active: stopping it first")
            obs.call("stop_record")
            time.sleep(2.0)
        obs.call("start_record")
        for _ in range(40):
            st = obs.call("get_record_status")
            if st is not None and st.output_active:
                break
            time.sleep(0.25)
        d.rec_t0 = time.time()
        d.log_rows[0]["rec_t"] = 0.0
        log(f"recording ({'active' if st is not None and st.output_active else 'NOT CONFIRMED'})")

    rec_pending = not a.no_record and a.record_on_run and a.follow and not a.replay and not a.run
    if rec_pending:
        log("armed: recording starts when the loop's run dir appears")
    elif not a.no_record:
        start_rec()
    if a.replay:
        threading.Thread(target=feeder, args=(a.replay, d.run, a.speed, a.from_tick, a.end_tick, feed_done, a.skip),
                         daemon=True).start()
    last_wd, last_status = 0.0, 0.0
    rc = 0
    try:
        while True:
            try:
                if a.follow and not a.replay:
                    nr = newest_run(RUNS)
                    if nr and nr != d.run and nr != start_newest and (d.run is None or nr > d.run):
                        d.attach(nr)
                        if rec_pending:
                            rec_pending = False
                            start_rec()
                if d.run:
                    for r in d.poll_ticks():
                        d.on_tick(r)
                    if d.final_t is None and d.eod is None and d.raw and TL.run_summary(d.run, d.raw):
                        d.finish("run finished (summary.md)")
                if os.path.exists(STOP_FILE) and d.final_t is None:
                    os.remove(STOP_FILE)
                    d.finish("stop requested")
                if a.replay and feed_done.is_set() and d.final_t is None and d.eod is None:
                    d.finish("replay fed")
                if not d.step():
                    break
                now = time.time()
                if now - last_wd > 5:
                    last_wd = now
                    watchdog()
                if now - last_status > 30 and not a.no_record and d.rec_t0:
                    last_status = now
                    st = obs.call("get_record_status")
                    if st is not None and not st.output_active:
                        log("WARNING: OBS reports the recording is not active")
                if now - t0 > a.max_minutes * 60:
                    log("max minutes reached")
                    d.finish("max minutes")
            except Exception as e:   # never die mid-take
                log(f"poll error: {e!r}")
            time.sleep(a.poll)
    except KeyboardInterrupt:
        log("interrupted")
        rc = 3
    if not a.no_record:
        r = None
        for _ in range(10):
            r = obs.call("stop_record")
            if r is not None:
                break
            time.sleep(1.0)
        d.output_path = getattr(r, "output_path", None)
        d.rec_stop = time.time()
        log(f"recording stopped -> {d.output_path}")
    d.write_log()
    log(f"scene log {os.path.join(d.run or '', 'scene_log.json')}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
