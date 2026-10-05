"""Turn a run's tick log into Cap zoom segments (zoomed IN on the game / OUT on the full desktop), alternating.

  python tools/cap_zoom_plan.py runs/<ts> --rects runs/<ts>/stage_rects.json --cap-project "<...>.cap" --record-start-from-cap
       (the demo: Cap recorded the "TOD stage" window, tools/stage.py; the frame is that window, no crop:
        IN = tile A (game mirror), TWOUP = whole window, TOD = sidebar tiles, TALLY = tile C)
  python tools/cap_zoom_plan.py runs/<ts> --rects runs/rects.json --cap-project "<...>.cap" [--record-start-from-cap]
  python tools/cap_zoom_plan.py runs/<ts> --game-rect 0,0,2280,1280 --display 3456x2160 --record-start 2026-10-04T11:57:00

Inputs
  run dir      runs/<ts>/tick_NNNN.json: rec["time"] (unix wall clock, s), rec["gt"] {screen, day, day_processed},
               rec["state"] (TOD answers), rec["manual_step_for_state"] ([step, text]).
  geometry     --rects runs/<ts>/stage_rects.json (tools/stage.py; "stage": true -> frame = the window, crop none),
               --rects <json from tools/window_rects.py> (uses display.rect + game_in_display) or the
               runs/demo_<ts>.json tools/demo_run.py writes (client + screen, primary display), or
               --game-rect x,y,w,h (display-relative physical px) + --display WxH.
  record start --record-start <ISO, local time if no offset>, or --record-start-from-cap: the bundle's
               recording-logs.log "Start gate admitted first video frame" UTC timestamp (fallback: bundle creation
               time). --offset S nudges every boundary (+ = later in the video).

Plan (per day, entrants = gt.day_processed values while gt.screen == DayScreen)
  - the intro before Day 1's booth (title / load / news) belongs to the first IN block;
  - entrants cycle per day: --in N IN (game), --twoup M TWOUP (full crop), --tod K TOD (sidebar: tile1 for the
    rest of the span (>= --tod-t1), then tile2 for --tod-t2; two contiguous manual segments so Cap pans);
    the no-docs entrant is split: first half TWOUP, second half TOD;
  - forced OUT: any entrant with a no-documents beat (manual step N1..N5, interrogate prompt visible, or
    no_documents_presented=yes on >= --nodocs-min-run consecutive ticks) -> the Day 3 Jorji sequence;
  - forced OUT: end of day = from --eod-lead s before the first non-DayScreen tick after the booth until the next
    day's first tick (the night summary is shown with the viewer); the last day stays OUT to the end;
  - a new day (News/Day screen) restarts the pattern with an IN block;
  - tally beats (<run_dir>/tally_schedule.json or --tally-schedule; [{start, end, reason}]) override everything:
    TALLY = OUT, or a manual zoom on --tally-rect; an IN right before a tally is cut --eod-lead s early, so a
    day end runs IN -> OUT (panel) -> TALLY -> next day IN.
OUT = no zoom segment (Cap shows the whole recorded display); IN = one Cap zoomSegment, mode manual{x,y}, amount
fitted so the game client rect fills the frame. Adjacent same-mode spans are merged.

Cap schema (CapSoftware/Cap crates/project/src/configuration.rs, ZoomSegment / ZoomMode, serde camelCase):
  {"start": f64, "end": f64, "amount": f64, "mode": "auto" | {"manual": {"x": f32, "y": f32}},
   "glideDirection": "none", "glideSpeed": 0.5, "instantAnimation": false, "edgeSnapRatio": 0.25}
  start/end are TIMELINE seconds. Recording seconds are mapped through timeline.segments (trims / speed) so you may
  trim in Cap before running this. Manual center c (crates/rendering/src/zoom.rs SegmentBounds::from_center):
  visible = [c(a-1)/a, (c(a-1)+1)/a] of the display, so c = (v*a - 0.5)/(a-1) for a visible center v.

--cap-project writes project-config.json in place (copy to project-config.json.bak-<ts> first). Close the Cap
editor for that recording before running (the editor keeps its own copy and saves over the file).
--raw-frame (default) also sets background padding/rounding/shadow/border to 0/none and background.crop to
--crop (default: the top 16:9 band of the display, 0,0,3456x1944 on 3456x2160; "none" = no crop). Zoom
amounts/anchors are computed inside that crop (Cap zooms the cropped frame).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone

DAY_SCREEN = "DayScreen"


# ----------------------------------------------------------------------------------------------------------------- input
def load_ticks(run_dir: str) -> list[dict]:
    out = []
    for f in sorted(glob.glob(os.path.join(run_dir, "tick_*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                r = json.load(fh)
        except (OSError, ValueError):
            continue
        if "time" not in r:
            continue
        g = r.get("gt") or {}
        st = r.get("state") or {}

        def val(k):
            x = st.get(k)
            return x.get("value") if isinstance(x, dict) else x
        step = (r.get("manual_step_for_state") or [""])[0] or ""
        out.append({
            "tick": r.get("tick"), "t": float(r["time"]),
            "screen": g.get("screen") if g.get("ok", True) else None,
            "day": g.get("day"), "proc": g.get("day_processed"),
            "nd": val("no_documents_presented") in (True, "yes"),
            "interrogate": val("interrogate_prompt_visible") in (True, "yes"),
            "nstep": bool(re.match(r"N\d", str(step))), "step": str(step),
        })
    out.sort(key=lambda x: x["t"])
    if not out:
        sys.exit(f"no tick_*.json with 'time' under {run_dir}")
    # gt can be briefly unreadable: carry the last known screen/day/processed forward
    last = {"screen": None, "day": None, "proc": None}
    for x in out:
        for k in last:
            if x[k] is None:
                x[k] = last[k]
            else:
                last[k] = x[k]
    return out


def parse_iso(s: str) -> float:
    s = s.strip().replace("Z", "+00:00")
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.astimezone()   # naive -> this machine's local time
    return d.timestamp()


def cap_record_start(bundle: str) -> tuple[float, str]:
    log = os.path.join(bundle, "recording-logs.log")
    if os.path.exists(log):
        with open(log, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "screen-out" in line and "admitted first video frame" in line:
                    return parse_iso(line.split()[0]), "recording-logs.log first admitted screen frame"
            fh.seek(0)
            for line in fh:
                if "pipeline playing" in line:
                    return parse_iso(line.split()[0]), "recording-logs.log 'pipeline playing'"
    st = os.stat(bundle)
    return getattr(st, "st_birthtime", st.st_ctime), "bundle creation time (approx, +-1-2 s)"


def load_geometry(a) -> tuple[tuple, tuple]:
    if a.rects:
        with open(a.rects, encoding="utf-8") as fh:
            R = json.load(fh)
        if "game_in_display" in R:            # tools/window_rects.py
            disp = tuple(R["display"]["rect"][2:4])
            game = tuple(R["game_in_display"])
        else:                                  # runs/demo_<ts>.json from tools/demo_run.py (primary display)
            disp, game = tuple(R["screen"]), tuple(R["client"])
    else:
        if not (a.game_rect and a.display):
            sys.exit("need --rects <window_rects.json> or both --game-rect x,y,w,h and --display WxH")
        game = tuple(int(v) for v in a.game_rect.split(","))
        disp = tuple(int(v) for v in a.display.lower().split("x"))
    if a.game_rect:
        game = tuple(int(v) for v in a.game_rect.split(","))
    if a.display:
        disp = tuple(int(v) for v in a.display.lower().split("x"))
    if game[2] <= 0 or game[3] <= 0 or game[0] < 0 or game[1] < 0 \
            or game[0] + game[2] > disp[0] or game[1] + game[3] > disp[1]:
        sys.exit(f"game rect {game} is not inside the {disp[0]}x{disp[1]} display (minimized / other monitor?)")
    return game, disp


def zoom_for(game, disp, crop=None, margin=0.0):
    """amount + manual center so the game rect (+margin px) fills the frame. crop = Cap background.crop (px)."""
    ox, oy, dw, dh = (crop["position"]["x"], crop["position"]["y"], crop["size"]["x"], crop["size"]["y"]) \
        if crop else (0, 0, disp[0], disp[1])
    gx, gy, gw, gh = game[0] - ox - margin, game[1] - oy - margin, game[2] + 2 * margin, game[3] + 2 * margin
    a = min(dw / gw, dh / gh)
    if a <= 1.0001:
        return 1.0, (0.5, 0.5)
    c = []
    for v in ((gx + gw / 2) / dw, (gy + gh / 2) / dh):
        c.append(min(1.0, max(0.0, (v * a - 0.5) / (a - 1))))
    return a, (c[0], c[1])


# ------------------------------------------------------------------------------------------------------------------ plan
def nodocs_entrants(ticks, min_run: int) -> set:
    """(day, processed) keys of entrants with a no-documents / interrogate beat."""
    keys, run = set(), []
    for x in ticks + [None]:
        if x is not None and x["screen"] == DAY_SCREEN and x["nd"] and (not run or run[-1]["proc"] == x["proc"]):
            run.append(x)
            continue
        if len(run) >= min_run:
            keys.add((run[0]["day"], run[0]["proc"]))
        run = [x] if x is not None and x["screen"] == DAY_SCREEN and x["nd"] else []
    for x in ticks:
        if x["screen"] == DAY_SCREEN and (x["nstep"] or x["interrogate"]):
            keys.add((x["day"], x["proc"]))
    return keys


def build_spans(ticks, n_in: int, n_out: int, n_tod: int, eod_lead: float, nodocs: set, t_end: float, min_span: float = 15.0):
    """[(t0, t1, mode, reason)] in wall-clock seconds, contiguous, from the first tick to t_end."""
    # group ticks into phases: ("pre", day) before the booth, ("ent", day, k), ("eod", day) after the booth
    phases = []   # [kind, day, k, t0]
    seen_booth = set()
    for x in ticks:
        d = x["day"]
        if x["screen"] == DAY_SCREEN:
            seen_booth.add(d)
            key = ("ent", d, x["proc"] or 0)
        elif d in seen_booth:
            key = ("eod", d, None)
        else:
            key = ("pre", d, None)
        if not phases or tuple(phases[-1][:3]) != key:
            phases.append([*key, x["t"]])
    spans = []
    cyc, cyc_day = 0, None   # position in the IN/TWOUP/TOD cycle; restarts each day and after a forced beat
    for i, (kind, d, k, t0) in enumerate(phases):
        t1 = phases[i + 1][3] if i + 1 < len(phases) else t_end
        if kind == "pre":
            spans.append([t0, t1, "in", f"day {d} intro (news)" if d else "intro (title/load)"])
        elif kind == "eod":
            spans.append([t0, t1, "out", f"day {d} end-of-day"])
        else:
            if (d, k) in nodocs:
                mid = (t0 + t1) / 2   # forced: first half TWOUP, second half TOD (the sidebar explains the beat)
                why = f"day {d} entrant #{k + 1}: NO DOCUMENTS / interrogate (forced)"
                spans.append([t0, mid, "out", why])
                spans.append([mid, t1, "tod", why])
                cyc = 0
            else:
                if cyc_day != d:
                    cyc, cyc_day = 0, d
                j = cyc % (n_in + n_out + n_tod)
                cyc += 1
                mode = "in" if j < n_in else "out" if j < n_in + n_out else "tod"
                spans.append([t0, t1, mode, f"day {d} entrant #{k + 1}"])
    # end-of-day lead: pull each eod OUT earlier by eod_lead (eats the tail of the previous span)
    for i in range(1, len(spans)):
        if spans[i][3].endswith("end-of-day") and eod_lead > 0 and spans[i - 1][2] in ("in", "tod"):
            cut = max(spans[i - 1][0], spans[i][0] - eod_lead)
            spans[i - 1][1] = spans[i][0] = cut
    spans = [s for s in spans if s[1] - s[0] > 1e-3]
    # a short pattern span (e.g. the stub after the last stamp before the night screen) inherits the previous mode
    for i in range(1, len(spans)):
        s = spans[i]
        if s[1] - s[0] < min_span and re.fullmatch(r"day \S+ entrant #\d+", s[3]):
            s[2] = spans[i - 1][2]
    return spans


def load_tally(path: str, t_end: float) -> list[tuple]:
    """Tally windows [(t0, t1, reason)] in unix s. Accepts a list of {start, end, reason} or {"windows"/"shown": [...]};
    start/end are ISO strings or unix floats; end null/missing = still up at the end of the recording."""
    with open(path, encoding="utf-8") as fh:
        J = json.load(fh)
    if isinstance(J, dict):
        J = J.get("windows") or J.get("shown") or J.get("schedule") or []

    def ts(v):
        return None if v in (None, "") else float(v) if isinstance(v, (int, float)) else parse_iso(str(v))
    out = []
    for w in J:
        s, e = ts(w.get("start")), ts(w.get("end"))
        if s is not None:
            r = w.get("rect")
            out.append((s, e if e is not None else t_end, str(w.get("reason") or "tally"),
                        tuple(int(v) for v in r) if r else None))
    return sorted(out)


def overlay_tally(spans, wins, pre_out: float):
    """Cut each tally window into the (unmerged) span list as mode 'tally'. An IN span right before a tally is
    shortened so at least `pre_out` s of OUT (panel) precede the tally: IN -> OUT -> tally -> next day IN."""
    for s, e, why, *_ in wins:
        new = []
        for sp in spans:
            if sp[1] <= s or sp[0] >= e:
                new.append(sp)
                continue
            if sp[0] < s:
                new.append([sp[0], s, sp[2], sp[3]])
            if sp[1] > e:
                new.append([e, sp[1], sp[2], sp[3]])
        new.append([s, e, "tally", f"tally: {why}"])
        new.sort(key=lambda x: x[0])
        i = next(k for k, x in enumerate(new) if x[2] == "tally" and x[0] == s)
        if i > 0 and new[i - 1][2] in ("in", "tod") and pre_out > 0:
            p = new[i - 1]
            cut = max(p[0], s - pre_out)
            new.insert(i, [cut, s, "out", "pre-tally (panel)"])
            p[1] = cut
        elif i > 1 and new[i - 1][2] == "out" and new[i - 1][1] - new[i - 1][0] < pre_out                 and new[i - 2][2] in ("in", "tod") and pre_out > 0:   # too-short TWOUP before the tally: widen it
            cut = max(new[i - 2][0], s - pre_out)
            new[i - 2][1] = new[i - 1][0] = min(cut, new[i - 1][0])
        spans = [x for x in new if x[1] - x[0] > 1e-3]
    return spans


def merge(spans):
    out = []
    for s in spans:
        if out and out[-1][2] == s[2]:
            out[-1][1] = s[1]
            out[-1][3].append(s[3])
        else:
            out.append([s[0], s[1], s[2], [s[3]]])
    return out


def compress_reasons(rs: list[str]) -> str:
    """'day 3 entrant #1', 'day 3 entrant #2' -> 'day 3 entrants #1-2'."""
    out, i = [], 0
    while i < len(rs):
        m = re.fullmatch(r"day (\S+) entrant #(\d+)", rs[i])
        if m:
            j = i
            while j + 1 < len(rs) and re.fullmatch(rf"day {re.escape(m[1])} entrant #{int(m[2]) + j - i + 1}", rs[j + 1]):
                j += 1
            out.append(f"day {m[1]} entrant{'s' if j > i else ''} #{m[2]}" + (f"-{int(m[2]) + j - i}" if j > i else ""))
            i = j + 1
        else:
            out.append(rs[i])
            i += 1
    return "; ".join(out)


# ------------------------------------------------------------------------------------------------------------- cap side
def rec_to_timeline(r: float, segs: list[dict] | None):
    """Recording seconds -> timeline seconds through timeline.segments (None if trimmed away)."""
    if not segs:
        return r
    acc = 0.0
    for s in segs:
        ts = s.get("timescale") or 1.0
        if s.get("recordingSegment", s.get("recordingClip", 0)) == 0 and s["start"] <= r <= s["end"]:
            return acc + (r - s["start"]) / ts
        acc += (s["end"] - s["start"]) / ts
    return None


def map_span(t0: float, t1: float, segs):
    """Clip a recording-time span to the kept timeline parts -> list of (tl0, tl1)."""
    if not segs:
        return [(t0, t1)]
    out, acc = [], 0.0
    for s in segs:
        ts = s.get("timescale") or 1.0
        a, b = max(t0, s["start"]), min(t1, s["end"])
        if s.get("recordingSegment", s.get("recordingClip", 0)) == 0 and b > a:
            out.append((acc + (a - s["start"]) / ts, acc + (b - s["start"]) / ts))
        acc += (s["end"] - s["start"]) / ts
    return out


def zoom_segment(start, end, amount, cx, cy, instant=False):
    return {"start": round(start, 3), "end": round(end, 3), "amount": round(amount, 4),
            "mode": {"manual": {"x": round(cx, 5), "y": round(cy, 5)}},
            "glideDirection": "none", "glideSpeed": 0.5, "instantAnimation": bool(instant), "edgeSnapRatio": 0.25}


def raw_frame(cfg: dict, crop: dict | None) -> list[str]:
    bg = cfg.setdefault("background", {})
    changed = []
    for k, v in (("padding", 0.0), ("rounding", 0.0), ("shadow", 0.0), ("inset", 0), ("blur", 0.0),
                 ("border", None), ("crop", crop)):
        if bg.get(k) != v:
            bg[k] = v
            changed.append(f"background.{k}={v}")
    if isinstance(bg.get("advancedShadow"), dict) and bg["advancedShadow"].get("opacity"):
        bg["advancedShadow"]["opacity"] = 0.0
        changed.append("background.advancedShadow.opacity=0")
    if cfg.get("aspectRatio") is not None:
        cfg["aspectRatio"] = None
        changed.append("aspectRatio=null (auto = source)")
    return changed


# ------------------------------------------------------------------------------------------------------------------ main
def fmt(s: float) -> str:
    s = max(0.0, s)
    return f"{int(s // 60):02d}:{s % 60:05.2f}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Cap zoom plan from a TOD run's tick log",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("run_dir")
    ap.add_argument("--rects", help="JSON written by tools/window_rects.py --out")
    ap.add_argument("--game-rect", help="x,y,w,h of the game client, display-relative physical px (overrides --rects)")
    ap.add_argument("--display", help="WxH of the recorded display, physical px (overrides --rects)")
    ap.add_argument("--record-start", help="wall-clock time of video t=0 (ISO; naive = local time)")
    ap.add_argument("--record-start-from-cap", nargs="?", const="", metavar="BUNDLE",
                    help="read t=0 from a .cap bundle (default: the --cap-project bundle)")
    ap.add_argument("--offset", type=float, default=0.0, help="seconds added to every boundary (sync nudge)")
    ap.add_argument("--in", dest="n_in", type=int, default=2, help="entrants per zoomed-IN block (default 2)")
    ap.add_argument("--twoup", "--out", dest="n_out", type=int, default=1,
                    help="entrants per TWOUP block (full crop: game + sidebar; default 1)")
    ap.add_argument("--tod", dest="n_tod", type=int, default=1,
                    help="entrants per TOD block (sidebar tile1 then tile2; default 1, 0 = never)")
    ap.add_argument("--tiles", help="sidebar tiles 'x,y,w,h;x,y,w,h;...' (display px; default: rects JSON 'tiles', "
                                    "else 3 stacked tiles right of the game)")
    ap.add_argument("--tod-t1", type=float, default=20.0, help="min hold on tile1 per TOD beat (s, default 20)")
    ap.add_argument("--tod-t2", type=float, default=12.0, help="hold on tile2 at the end of a TOD beat (s, default 12)")
    ap.add_argument("--eod-lead", type=float, default=8.0,
                    help="zoom out this many s before the end-of-day screen (default 8)")
    ap.add_argument("--nodocs-min-run", type=int, default=3,
                    help="consecutive no_documents_presented=yes ticks that count as a no-docs beat (default 3)")
    ap.add_argument("--lead", type=float, default=0.3,
                    help="start each zoom change this many s early (Cap animates ~0.5-1 s; default 0.3)")
    ap.add_argument("--margin", type=float, default=0.0, help="px of desktop kept around the game when zoomed IN")
    ap.add_argument("--min-span", type=float, default=15.0,
                    help="an entrant span shorter than this (s) keeps the previous zoom (default 15)")
    ap.add_argument("--min-in",type=float, default=2.0, help="drop IN segments shorter than this (s)")
    ap.add_argument("--duration", type=float, help="video length (s); default: Cap timeline, else last tick + 5 s")
    ap.add_argument("--cap-project", help=".cap bundle dir or its project-config.json: write zoomSegments there")
    ap.add_argument("--crop", default="auto", help="background.crop x,y,w,h | auto (top 16:9 of the display) | none")
    ap.add_argument("--keep-style", action="store_true", help="do not force padding/rounding/shadow/crop to raw")
    ap.add_argument("--merge", action="store_true", help="keep existing zoomSegments outside our IN spans "
                                                          "(default: replace them all, incl. Cap's auto-zoom ones)")
    ap.add_argument("--tally-schedule", help="tally windows JSON (default: <run_dir>/tally_schedule.json if present):"
                                             " list of {start, end, reason}, ISO or unix s; end null = to the end")
    ap.add_argument("--tally-rect", help="x,y,w,h (display-relative px): zoom on the tally window while it is up "
                                         "(default: --rects tally_in_display, else OUT)")
    ap.add_argument("--json", help="also write the plan (spans + segments) to this file")
    a = ap.parse_args(argv)

    ticks = load_ticks(a.run_dir)
    game, disp = load_geometry(a)

    cfg_path = cfg = None
    if a.cap_project:
        cfg_path = a.cap_project if a.cap_project.endswith(".json") else os.path.join(a.cap_project, "project-config.json")
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    bundle = os.path.dirname(cfg_path) if cfg_path else None

    if a.record_start:
        t0, src = parse_iso(a.record_start), "--record-start"
    elif a.record_start_from_cap is not None:
        b = a.record_start_from_cap or bundle
        if not b:
            sys.exit("--record-start-from-cap needs a bundle path or --cap-project")
        t0, src = cap_record_start(b)
    else:
        t0, src = ticks[0]["t"], "first tick (no --record-start given: times are relative to the run start)"
    t0 -= a.offset

    segs = (cfg or {}).get("timeline", {}) or {}
    segs = segs.get("segments") if isinstance(segs, dict) else None
    rec_len = a.duration or (max(s["end"] for s in segs) if segs else (ticks[-1]["t"] - t0 + 5.0))
    t_end = t0 + rec_len

    nodocs = nodocs_entrants(ticks, a.nodocs_min_run)
    spans = build_spans(ticks, a.n_in, a.n_out, a.n_tod, a.eod_lead, nodocs, t_end, a.min_span)
    tally_path = a.tally_schedule or os.path.join(a.run_dir, "tally_schedule.json")
    tally = load_tally(tally_path, t_end) if os.path.exists(tally_path) else []
    if a.tally_schedule and not tally:
        print(f"WARNING: no tally windows in {a.tally_schedule}")
    spans = merge(overlay_tally(spans, tally, a.eod_lead))
    need = a.tod_t1 + a.tod_t2   # a TOD beat needs tile1 >= T1 then tile2 = T2: extend into the next span if short
    for i, sp in enumerate(spans):
        if sp[2] == "tod" and sp[1] - sp[0] < need and i + 1 < len(spans) and spans[i + 1][2] != "tally" \
                and not any("end-of-day" in r for r in spans[i + 1][3]):
            nxt = spans[i + 1]
            ext = min(sp[0] + need, nxt[1] - 1.0)
            if ext > sp[1]:
                sp[1] = nxt[0] = ext
    tally_rect = tuple(int(v) for v in a.tally_rect.split(",")) if a.tally_rect else None
    if tally_rect is None and a.rects:
        with open(a.rects, encoding="utf-8") as fh:
            tally_rect = tuple(json.load(fh).get("tally_in_display") or ()) or None
    # video starts zoomed IN: the first span is stretched back to t=0 (Cap's countdown / setup before tick 0)
    if spans and spans[0][0] > t0:
        spans[0][0] = t0
    stage = False
    if a.rects:
        with open(a.rects, encoding="utf-8") as fh:
            stage = bool(json.load(fh).get("stage"))
    if a.keep_style:
        crop = (cfg or {}).get("background", {}).get("crop")
    elif a.crop.lower() == "none" or (stage and a.crop == "auto"):   # stage window = the whole frame
        crop = None
    else:
        cx_, cy_, cw_, ch_ = (int(v) for v in a.crop.split(",")) if a.crop != "auto" \
            else (0, 0, disp[0], min(disp[1], round(disp[0] * 9 / 16)))
        crop = {"position": {"x": cx_, "y": cy_}, "size": {"x": cw_, "y": ch_}}   # Crop {position, size: XY<u32>}
    amount, (cx, cy) = zoom_for(game, disp, crop, a.margin)
    tiles = None
    if a.tiles:
        tiles = [tuple(int(v) for v in t.split(",")) for t in a.tiles.split(";")]
    elif a.rects:
        with open(a.rects, encoding="utf-8") as fh:
            tiles = [tuple(t) for t in (json.load(fh).get("tiles") or [])] or None
    if not tiles:   # default: three stacked 16:9 tiles in the sidebar column right of the game
        fw, fh_ = (crop["size"]["x"], crop["size"]["y"]) if crop else disp
        sx = game[0] + game[2]
        tiles = [(sx, i * fh_ // 3, fw - sx, fh_ // 3) for i in range(3)]
    tile_z = [zoom_for(t, disp, crop, 0.0) for t in tiles[:2]]
    t_amount, (tcx, tcy) = zoom_for(tally_rect, disp, crop, a.margin) if tally_rect else (1.0, (0.5, 0.5))

    def tally_zoom(mid):   # per-window rect from the schedule wins over --tally-rect / --rects
        w = next((w for w in tally if w[0] <= mid <= w[1] and w[3]), None)
        if w:
            am, (x, y) = zoom_for(w[3], disp, crop, a.margin)
            return am, x, y
        return t_amount, tcx, tcy
    print("crop: " + (json.dumps(crop) if crop else "none"))
    print("TOD tiles: " + "; ".join(f"{t} -> amount {z[0]:.3f} anchor ({z[1][0]:.3f}, {z[1][1]:.3f})"
                                   for t, z in zip(tiles, tile_z))
          + f"; tile1 >= {a.tod_t1:g}s (rest of the TOD span), tile2 {a.tod_t2:g}s")
    print(f"tally: {len(tally)} window(s) from {tally_path if tally else '-'}; "
          + (f"{sum(1 for w in tally if w[3])} with their own rect; " if tally else "")
          + (f"default: zoom on {tally_rect} (amount {t_amount:.3f})" if t_amount > 1.0001 else "default: zoomed OUT"))

    print(f"run {a.run_dir}: {len(ticks)} ticks, record start = {datetime.fromtimestamp(t0).isoformat(timespec='milliseconds')}"
          f" ({src}{', offset %+.2fs' % a.offset if a.offset else ''})")
    print(f"display {disp[0]}x{disp[1]}, game rect {game} -> IN = amount {amount:.3f}, center ({cx:.4f}, {cy:.4f});"
          f" pattern {a.n_in} IN / {a.n_out} TWOUP / {a.n_tod} TOD; no-docs entrants: "
          + (", ".join(f"day {d} #{k + 1}" for d, k in sorted(nodocs, key=lambda z: (z[0] or 0, z[1] or 0))) or "none"))
    if t0 > ticks[-1]["t"] or t_end < ticks[0]["t"]:
        print("WARNING: the run does not overlap the recording window -- wrong run dir or record start?")
    print(f"\n{'video start':>11} {'video end':>10} {'dur':>7}  zoom  reason")
    out_segments, rows = [], []
    for s0, s1, mode, why in spans:
        r0, r1 = max(0.0, s0 - t0), min(rec_len, s1 - t0)
        if r1 <= r0:
            continue
        reason = compress_reasons(why)
        if mode == "tod":
            split = max(r0 + min(a.tod_t1, (r1 - r0) / 2), r1 - a.tod_t2)
            for q0, q1, nm, z in ((r0, split, "tile1", tile_z[0]), (split, r1, "tile2", tile_z[-1])):
                rows.append({"start": round(q0, 3), "end": round(q1, 3), "zoom": "tod-" + nm, "reason": reason})
                print(f"{fmt(q0):>11} {fmt(q1):>10} {q1 - q0:6.1f}s  TOD   {reason} [{nm}]")
                z0 = q0 if nm == "tile2" else max(0.0, q0 - a.lead)      # tile1 -> tile2 contiguous: Cap pans
                z1 = q1 if nm == "tile1" else max(z0 + 0.1, q1 - a.lead)
                for tl0, tl1 in map_span(z0, z1, segs):
                    out_segments.append(zoom_segment(tl0, tl1, z[0], z[1][0], z[1][1]))
            continue
        rows.append({"start": round(r0, 3), "end": round(r1, 3), "zoom": mode, "reason": reason})
        label = {"out": "TWOUP"}.get(mode, mode.upper())
        print(f"{fmt(r0):>11} {fmt(r1):>10} {r1 - r0:6.1f}s  {label:<5} {reason}")
        za = (amount, cx, cy) if mode == "in" else tally_zoom((s0 + s1) / 2) if mode == "tally" else None
        if za and za[0] > 1.0001 and r1 - r0 >= (a.min_in if mode == "in" else 0.5):
            z0 = r0 if r0 <= 0.01 else max(0.0, r0 - a.lead)
            z1 = r1 if r1 >= rec_len - 0.01 else max(z0 + 0.1, r1 - a.lead)
            for tl0, tl1 in map_span(z0, z1, segs):
                out_segments.append(zoom_segment(tl0, tl1, za[0], za[1], za[2], instant=tl0 <= 0.01))
    tot = {}
    for r in rows:
        k = r["zoom"].split("-")[0]
        tot[k] = tot.get(k, 0) + r["end"] - r["start"]
    print("\n" + f"{len(out_segments)} zoom segments; " + ", ".join(
        f"{({'out': 'TWOUP'}).get(k, k.upper())} {v / 60:.1f} min" for k, v in tot.items()) + f" of {rec_len / 60:.1f} min")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({"record_start": t0, "record_start_src": src, "display": disp, "game_rect": game,
                       "amount": amount, "center": [cx, cy], "spans": rows, "zoomSegments": out_segments}, fh, indent=2)
        print(f"plan -> {a.json}")

    if cfg is not None:
        bak = f"{cfg_path}.bak-{time.strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(cfg_path, bak)
        tl = cfg.get("timeline")
        if not isinstance(tl, dict):
            tl = cfg["timeline"] = {"segments": [{"recordingSegment": 0, "timescale": 1.0, "start": 0.0,
                                                  "end": round(rec_len, 3), "name": None}]}
            print("NOTE: project had no timeline; created one segment 0..%.1fs" % rec_len)
        keep = []
        if a.merge:
            for z in tl.get("zoomSegments") or []:
                if all(z["end"] <= o["start"] or z["start"] >= o["end"] for o in out_segments):
                    keep.append(z)
        tl["zoomSegments"] = sorted(keep + out_segments, key=lambda z: z["start"])
        changed = [] if a.keep_style else raw_frame(cfg, crop)
        tmp = cfg_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        os.replace(tmp, cfg_path)
        print(f"wrote {len(tl['zoomSegments'])} zoomSegments ({len(keep)} kept) -> {cfg_path}\nbackup -> {bak}")
        if changed:
            print("raw frame: " + ", ".join(changed))
        print("now reopen the recording in Cap (Recordings -> this one -> Edit) and check the zoom track")
    return 0


if __name__ == "__main__":
    sys.exit(main())
