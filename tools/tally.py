"""Tally / scoreboard screen for the demo recording (docs/tally.md).
  python tools/tally.py --run runs/<ts> --tick N [--out f.png]   render the tally as of tick N to a PNG
  python tools/tally.py --run runs/<ts> --numbers                 print the numbers (compare: tools/report.py)
  python tools/tally.py --run runs/<ts> --export [DIR]            per-day + final PNGs and tally_schedule.json
  python tools/tally.py --follow [--secs 12]                      hidden live window; pops on top (never takes
                                                                  focus) at each day end, stays up at the end
  python tools/tally.py --show-now [SECS]                         pop the running --follow window (signal file)
In --follow, F9 also toggles it. Read-only: polls the run dir like tools/viewer.py, never touches the loop/game.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from tod_papers import tally as TL  # noqa: E402
from tod_papers.viewer import newest_run  # noqa: E402

RUNS = os.path.join(ROOT, "runs")
SIGNAL = os.path.join(RUNS, ".tally_show_now")
SCHEDULE = "tally_schedule.json"


# ---------------------------------------------------------------- schedule file
class Schedule:
    """runs/<ts>/tally_schedule.json: JSON list of {"start": ISO, "end": ISO | null, "reason": str}, one entry per
    time the tally window was shown (for --export: when it would have been, from the tick times). end = null for
    the final screen that stays up. Rewritten atomically (tmp + os.replace) on every change."""

    def __init__(self, run_dir: str, fresh: bool = False):
        self.path = os.path.join(run_dir, SCHEDULE)
        self.items: list[dict] = []
        if not fresh and os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as fh:
                    doc = json.load(fh)
                self.items = doc if isinstance(doc, list) else []
            except (OSError, ValueError):
                self.items = []

    @staticmethod
    def iso(t: float | None) -> str | None:
        if t is None:
            return None
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"

    def add(self, start: float, end: float | None, reason: str) -> dict:
        e = {"start": self.iso(start), "end": self.iso(end), "reason": reason}
        self.items.append(e)
        self.save()
        return e

    def close(self, e: dict, t: float) -> None:
        if e.get("end") is None:
            e["end"] = self.iso(t)
            self.save()

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.items, fh, indent=1)
        os.replace(tmp, self.path)


# ---------------------------------------------------------------- export / png
def export(run_dir: str, out_dir: str, a) -> None:
    rt = TL.RunTicks(run_dir)
    rt.poll()
    if not rt.ticks:
        sys.exit(f"{run_dir}: no ticks")
    os.makedirs(out_dir, exist_ok=True)
    ren = TL.TallyRenderer()
    sch = Schedule(run_dir, fresh=True)
    events = TL.day_end_events(rt.ticks, a.final_day)
    finished = TL.run_summary(run_dir, rt.ticks) is not None
    by_tick = {r["tick"]: r for r in rt.ticks}
    written = []
    kw = {"final_day": a.final_day, "rate_per_1k": a.rate_per_1k, "unit": a.cost_unit}
    times = [r["time"] for r in rt.ticks]
    for e in events:
        final = e["kind"] == "final"
        # day-end tallies exactly as the live window drew them: before summary.md existed (live=True); the final one
        # as it stayed up after the run ended (summary.md's exact request count)
        T = TL.tally_for(run_dir, rt.ticks, None if final else e["tick"], live=not final, **kw)
        T["kind"] = e["kind"]
        name = "tally_final.png" if final else f"tally_day{e['day']}.png"
        ren.render(T, (a.width, a.height)).save(os.path.join(out_dir, name))
        t = by_tick[e["tick"]]["time"]
        sch.add(t, None if final else t + a.secs, f"{e['kind']} day {e['day']} tick {e['tick']}: {e['why']} -> {name}")
        written.append(name)
        if not final:
            # the live window repaints on every tick loaded while it is up (header tick, next day's first row):
            # tally_day<d>_t<NNNN>.png per such tick, for tools/patch_tally.py (re-skinning a recorded video)
            i0 = next(i for i, r in enumerate(rt.ticks) if r["tick"] == e["tick"])
            shown = times[i0 + 1] if i0 + 1 < len(times) else times[i0]   # loaded ~ when the next tick starts
            for i in range(i0, len(rt.ticks)):
                if i > i0 and (i + 1 >= len(times) or times[i + 1] > shown + a.secs + 3):   # +3 s: director slack
                    break
                k = rt.ticks[i]["tick"]
                T = TL.tally_for(run_dir, rt.ticks, k, live=True, **kw)
                T["kind"] = e["kind"]
                ren.render(T, (a.width, a.height)).save(os.path.join(out_dir, f"tally_day{e['day']}_t{k:04d}.png"))
    if finished and not any(e["kind"] == "final" for e in events):
        T = TL.tally_for(run_dir, rt.ticks, rate_per_1k=a.rate_per_1k, unit=a.cost_unit, final_day=a.final_day)
        T["kind"] = "final"
        ren.render(T, (a.width, a.height)).save(os.path.join(out_dir, "tally_final.png"))
        t = rt.ticks[-1]["time"]
        sch.add(t, None, f"final day {T['cur_day']} tick {rt.ticks[-1]['tick']}: run finished -> tally_final.png")
        written.append("tally_final.png")
    print(f"[tally] {len(written)} PNG(s) -> {out_dir}: {', '.join(written) or '-'}; schedule -> {sch.path}")


# ---------------------------------------------------------------- live window (Win32 no-activate topmost)
GWL_EXSTYLE = -20
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE, WS_EX_APPWINDOW = 0x8, 0x80, 0x08000000, 0x40000
WS_EX_LAYERED, WS_EX_TRANSPARENT, LWA_ALPHA = 0x80000, 0x20, 0x2   # layered+transparent = mouse clicks pass through
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW, SWP_HIDEWINDOW = 0x1, 0x2, 0x10, 0x40, 0x80
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
VK_F9 = 0x78


def _dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # physical pixels, same units as the game window
    except Exception:
        pass


def follow(a) -> None:
    import tkinter as tk
    from PIL import ImageTk

    _dpi_aware()
    u32 = ctypes.windll.user32
    u32.GetWindowLongW.restype = ctypes.c_long
    u32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_int] * 4 + [ctypes.c_uint]
    sw, sh = u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)
    W, H = a.width, a.height
    x = a.x if a.x is not None else (sw - W) // 2
    y = a.y if a.y is not None else (sh - H) // 2
    fg_before = u32.GetForegroundWindow()

    root = tk.Tk()
    root.withdraw()
    root.title("TOD tally")
    root.overrideredirect(True)
    root.geometry(f"{W}x{H}+{x}+{y}")
    root.configure(bg="#21241b")
    lbl = tk.Label(root, bd=0, bg="#21241b")
    lbl.pack(fill="both", expand=True)
    root.update_idletasks()
    hwnd = u32.GetParent(root.winfo_id()) or root.winfo_id()
    ex = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ex = (ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST) & ~WS_EX_APPWINDOW
    if a.click_through:
        ex |= WS_EX_LAYERED | WS_EX_TRANSPARENT
    u32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
    if a.click_through:
        u32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
    # map once (Tk must think the window is mapped so it keeps painting), then hide it with Win32 only
    root.deiconify()
    root.update()
    hwnd = u32.GetParent(root.winfo_id()) or hwnd
    u32.ShowWindow(hwnd, SW_HIDE)
    if fg_before and u32.GetForegroundWindow() != fg_before:
        u32.SetForegroundWindow(fg_before)   # we were foreground at start, so this is allowed

    ren = TL.TallyRenderer()
    st = {"run": None, "rt": None, "fired": set(), "visible": False, "until": None, "cur": None, "sch": None,
          "photo": None, "f9": False, "final": False, "img_key": None}

    def write_current(im) -> None:
        """runs/<ts>/tally/tally_current.png for an OBS Image source: the tally while it is up, else a 1x1
        transparent PNG. Written atomically (tmp + os.replace) so OBS never reads a half-written file."""
        if not st["run"]:
            return
        from PIL import Image as _Image
        d = os.path.join(st["run"], "tally")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "tally_current.png")
        (im if im is not None else _Image.new("RGBA", (1, 1), (0, 0, 0, 0))).save(p + ".tmp.png")
        os.replace(p + ".tmp.png", p)

    def attach(run_dir: str, initial: bool) -> None:
        st.update(run=run_dir, rt=TL.RunTicks(run_dir), fired=set(), final=False, img_key=None, cur_img=None)
        write_current(None)
        st["rt"].poll()
        st["dirty"] = True   # evaluate triggers on the ticks already loaded
        st["sch"] = Schedule(run_dir)
        if initial:   # events already in the log when we attached are history, not a cue
            st["fired"] = {(e["kind"], e["day"]) for e in TL.day_end_events(st["rt"].ticks, a.final_day)}
            if TL.run_summary(run_dir, st["rt"].ticks):
                st["fired"].add(("run_end", None))
        print(f"[tally] following {run_dir} ({len(st['rt'].ticks)} ticks)")

    def paint(kind: str | None) -> None:
        rt = st["rt"]
        if not rt or not rt.ticks:
            return
        T = TL.tally_for(st["run"], rt.ticks, final_day=a.final_day, rate_per_1k=a.rate_per_1k, unit=a.cost_unit)
        if kind:
            T["kind"] = kind
        key = (len(rt.ticks), T["finished"], kind)
        if key == st["img_key"]:
            return
        st["img_key"] = key
        im = ren.render(T, (W, H))
        st["cur_img"] = im
        st["photo"] = ImageTk.PhotoImage(im)
        lbl.configure(image=st["photo"])
        if st["visible"] or kind:   # shown (or about to be): OBS image = this tally
            write_current(im)

    def show(kind: str, day, tick, why: str, secs: float | None) -> None:
        paint("final" if kind in ("final", "run_end") else None)
        if st.get("cur_img") is not None:
            write_current(st["cur_img"])   # also when paint() hit its cache (same image as last time)
        u32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, W, H, SWP_NOACTIVATE | SWP_SHOWWINDOW)
        root.update_idletasks()
        now = time.time()
        if st["visible"] and st["cur"]:
            st["sch"].close(st["cur"], now)
        st["cur"] = st["sch"].add(now, None, f"{kind} day {day} tick {tick}: {why}")
        st["cur_kind"] = kind
        st["visible"], st["until"] = True, (now + secs) if secs else None
        print(f"[tally] show {kind} day={day} tick={tick} ({why})" + (f" for {secs:.0f}s" if secs else " (stays up)"))

    def hide() -> None:
        u32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE | SWP_HIDEWINDOW)
        if st["cur"]:
            st["sch"].close(st["cur"], time.time())
        st["visible"], st["until"], st["cur"] = False, None, None
        write_current(None)

    def manual(secs: float, why: str) -> None:
        rt = st["rt"]
        if st["visible"]:
            hide()
            return
        last = rt.ticks[-1] if rt and rt.ticks else {}
        g = last.get("gt") or {}
        show("manual", g.get("day"), last.get("tick"), why, secs)

    def tick() -> None:
        try:
            if a.follow:
                nr = newest_run(RUNS)
                if nr and nr != st["run"]:
                    attach(nr, initial=st["run"] is None and not a.fire_existing)
            rt = st["rt"]
            if rt and (rt.poll() or st.pop("dirty", False)):
                for e in TL.day_end_events(rt.ticks, a.final_day):
                    k = (e["kind"], e["day"])
                    if k not in st["fired"]:
                        st["fired"].add(k)
                        st["final"] = st["final"] or e["kind"] == "final"
                        show(e["kind"], e["day"], e["tick"], e["why"], None if e["kind"] == "final" else a.secs)
                if st["visible"]:
                    paint("final" if st["final"] else None)
            if rt and ("run_end", None) not in st["fired"] and TL.run_summary(st["run"], rt.ticks):
                st["fired"].add(("run_end", None))
                st["final"] = True
                last = rt.ticks[-1] if rt.ticks else {}
                if st["visible"] and st["cur"] and st.get("cur_kind") == "final":
                    paint("final")   # already up: refresh with summary.md's exact calls / cost
                else:
                    show("run_end", (last.get("gt") or {}).get("day"), last.get("tick"), "run finished", None)
            if os.path.exists(SIGNAL):
                try:
                    with open(SIGNAL, encoding="utf-8") as fh:
                        s = fh.read().strip()
                    os.remove(SIGNAL)
                except OSError:
                    s = ""
                secs = float(s) if s.replace(".", "", 1).isdigit() else a.secs
                if st["visible"] and st.get("cur_kind") in ("final", "run_end"):
                    secs = None   # the final screen is already up and stays: nothing to do
                elif st["visible"]:
                    hide()
                if secs:
                    last = rt.ticks[-1] if rt and rt.ticks else {}
                    show("manual", (last.get("gt") or {}).get("day"), last.get("tick"), "--show-now", secs)
            down = bool(u32.GetAsyncKeyState(VK_F9) & 0x8000)
            if down and not st["f9"] and a.hotkey:
                manual(a.secs, "F9")
            st["f9"] = down
            if st["visible"] and st["until"] and time.time() >= st["until"]:
                hide()
        except Exception as ex:   # never die mid-recording
            print(f"[tally] poll error: {ex!r}")
        root.after(int(a.poll * 1000), tick)

    if a.run:
        attach(a.run, initial=not a.fire_existing)
        a.follow = False
    root.after(50, tick)
    print(f"[tally] window {W}x{H}+{x}+{y} (screen {sw}x{sh}), hidden until a day ends / F9 / --show-now; Ctrl+C quits")
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    if st["cur"]:
        st["sch"].close(st["cur"], time.time())


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", help="run dir (runs/<ts>)")
    ap.add_argument("--follow", action="store_true", help="live: newest run under runs/, switch to newer ones")
    ap.add_argument("--tick", type=int, default=None, help="render the tally as of this tick to --out")
    ap.add_argument("--out", default=None, help="PNG path for --tick (default runs/<ts>/tally/tally_tNNNN.png)")
    ap.add_argument("--export", nargs="?", const="", default=None, metavar="DIR",
                    help="per-day + final PNGs (default DIR runs/<ts>/tally) + tally_schedule.json")
    ap.add_argument("--numbers", action="store_true", help="print the tally numbers as text")
    ap.add_argument("--show-now", nargs="?", const="", default=None, metavar="SECS",
                    help="pop the running --follow window for SECS (default its --secs) and exit")
    ap.add_argument("--secs", type=float, default=12.0, help="seconds a day-end tally stays up (final stays up)")
    ap.add_argument("--final-day", type=int, default=3, help="day whose end shows the final tally")
    ap.add_argument("--rate-per-1k", type=float, default=TL.RATE_PER_1K,
                    help="cost model: $ per 1,000 --cost-unit, default %(default)s")
    ap.add_argument("--cost-unit", choices=TL.R.UNITS, default=TL.COST_UNIT,
                    help="decisions = questions TOD answered (tod-api books decisions=len(questions)), "
                         "or requests (default %(default)s)")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--x", type=int, default=None, help="window left, physical px (default: centred on the primary display)")
    ap.add_argument("--y", type=int, default=None)
    ap.add_argument("--poll", type=float, default=0.25, help="live poll interval, s")
    ap.add_argument("--no-click-through", dest="click_through", action="store_false",
                    help="let the window take mouse clicks (default: clicks pass through to the game below)")
    ap.add_argument("--no-hotkey", dest="hotkey", action="store_false", help="disable F9")
    ap.add_argument("--fire-existing", action="store_true",
                    help="also pop for day ends already in the log when attaching (default: only new ones)")
    a = ap.parse_args(argv)

    if a.show_now is not None:
        os.makedirs(RUNS, exist_ok=True)
        with open(SIGNAL, "w", encoding="utf-8") as fh:
            fh.write(a.show_now)
        print(f"[tally] signalled {SIGNAL}")
        return 0
    if a.follow and not a.run:
        follow(a)
        return 0
    run_dir = a.run or newest_run(RUNS)
    if not run_dir:
        sys.exit("no run dir")
    if a.numbers:
        rt = TL.RunTicks(run_dir)
        rt.poll()
        T = TL.tally_for(run_dir, rt.ticks, a.tick, final_day=a.final_day, rate_per_1k=a.rate_per_1k, unit=a.cost_unit)
        print(f"Tally {os.path.basename(os.path.normpath(run_dir))}" + (f" @ tick {a.tick}" if a.tick is not None else ""))
        print(TL.numbers_text(T))
        return 0
    if a.export is not None:
        export(run_dir, a.export or os.path.join(run_dir, "tally"), a)
        return 0
    if a.tick is not None:
        rt = TL.RunTicks(run_dir)
        rt.poll()
        T = TL.tally_for(run_dir, rt.ticks, a.tick, final_day=a.final_day, rate_per_1k=a.rate_per_1k, unit=a.cost_unit)
        out = a.out or os.path.join(run_dir, "tally", f"tally_t{a.tick:04d}.png")
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        TL.TallyRenderer().render(T, (a.width, a.height)).save(out)
        print(f"[tally] {out}")
        return 0
    if a.run:
        follow(a)   # live window on one specific run
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
