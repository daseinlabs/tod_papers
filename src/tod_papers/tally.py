"""Tally / scoreboard screen for the demo recording (docs/tally.md).

Reads a run directory exactly as the viewer does (`tick_NNNN.json`, via viewer.RunReader / list_ticks) plus the
run's `summary.md` once the loop has finished, and renders a Papers, Please style end-of-day ledger with PIL:
one row per day plus a totals row, and a band of big cumulative numbers. The per-day grouping, correctness and
citation logic is tools/report.py's `analyze()` (imported, not copied), so the tally and `tools/report.py`
always print the same numbers. The window / follow / export logic lives in tools/tally.py.

Read-only: nothing here talks to the loop or the game.
"""
from __future__ import annotations

import importlib.util
import os
import statistics as st
import time

from PIL import Image, ImageDraw

from tod_papers.viewer import RunReader, _font, list_ticks

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_report():
    spec = importlib.util.spec_from_file_location("tod_report", os.path.join(ROOT, "tools", "report.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = _load_report()   # load / summary_md / tick_calls / analyze

# cost model: $ per 1,000 decisions (decision = one question TOD answered), API or own GPU box alike (report.py)
RATE_PER_1K, COST_UNIT = R.RATE_PER_1K, R.COST_UNIT


# ---------------------------------------------------------------- data
class RunTicks:
    """Incremental tick loader for a run dir (live or finished). Uses the viewer's cached, retrying reader."""

    def __init__(self, run_dir: str):
        self.run_dir = run_dir
        self.reader = RunReader(run_dir)
        self.ticks: list[dict] = []
        self._seen: set[int] = set()

    def poll(self) -> int:
        """Load newly written ticks (in order; stops at the first one still being written). Returns #new."""
        n = 0
        for t in list_ticks(self.run_dir):
            if t in self._seen:
                continue
            r = self.reader.rec(t)
            if r is None:
                break
            self._seen.add(t)
            self.ticks.append(r)
            n += 1
        return n

    def summary(self) -> dict | None:
        """summary.md numbers once the loop has written it (end of run), else None."""
        if not os.path.exists(os.path.join(self.run_dir, "summary.md")):
            return None
        sm = R.summary_md(self.run_dir)
        return sm if sm.get("calls") else None


def _usage(r: dict) -> tuple[float | None, int | None]:
    """Per-tick TOD usage if the loop logged it ({cost_usd, input_tokens} or a list of them); current runs don't."""
    u = r.get("tod_usage") or r.get("usage")
    if not u:
        return None, None
    us = u if isinstance(u, list) else [u]
    return (sum(float(x.get("cost_usd") or 0) for x in us), sum(int(x.get("input_tokens") or 0) for x in us))


def _executed(r: dict) -> str | None:
    e = (r.get("executed") or "").strip()
    if not e or e.startswith("vetoed") or e == "none":
        return None
    return e.split(" ")[0]


def _block(ticks: list[dict], scale: float, rate_per_1k: float, exact_calls: int | None = None,
           unit: str = COST_UNIT) -> dict:
    """Counters that only depend on a list of ticks (one day, or the whole run)."""
    calls = sum(R.tick_calls(r) for r in ticks) * scale
    if exact_calls:
        calls = exact_calls
    picks = [p for p in (_executed(r) for r in ticks) if p]
    us = [_usage(r) for r in ticks]
    tokens = sum(t for _, t in us if t) if any(t for _, t in us) else None
    times = [r["time"] for r in ticks if isinstance(r.get("time"), (int, float))]
    dt = [b - a for a, b in zip(times, times[1:])]
    # decisions = questions TOD answered (request-1/1b state answers + request-2 answers, each with probabilities);
    # cost = rate_per_1k * decisions / 1000 -- exact from the log, live or finished, API or own GPU box
    answers = sum(R.tick_answers(r) for r in ticks)
    cost = R.decision_cost(answers if unit == "decisions" else calls, rate_per_1k)
    return {
        "ticks": len(ticks),
        "answers": answers,
        "picks": len(picks),
        "picks_by": {k: picks.count(k) for k in ("click", "drag", "wait")},
        "calls": calls,
        "images": calls,   # every TOD request carries exactly one image (tod_client.ask)
        "cost": cost,
        "tokens": tokens,
        "wall_s": (times[-1] - times[0]) if len(times) > 1 else 0.0,
        "med_tick_ms": 1e3 * st.median(dt) if dt else None,
        "t0": times[0] if times else None,
        "t1": times[-1] if times else None,
    }


def compute(ticks: list[dict], sm: dict | None, full_run: bool = True,
            rate_per_1k: float = RATE_PER_1K, final_day: int = 3, unit: str = COST_UNIT) -> dict:
    """All tally numbers for `ticks` (a full run, or a prefix of one: `full_run=False`).
    sm = summary.md numbers (None while the loop is still running -> request counts are the log's own count).
    cost = rate_per_1k * decisions / 1000; summary.md's API bill (if > 0) is kept as tot["billed"] only."""
    have_sm = bool(sm and sm.get("calls"))
    # request scale from the FULL run (report.py apportions summary.md calls by the requests each tick made)
    scale = sm["_scale"] if have_sm else 1.0
    rate = rate_per_1k
    A = R.analyze(ticks, {"calls": None, "cost": None, "args": "", "stop": "-"}, rate, unit)
    days = {}
    for d, D in sorted(A["days"].items()):
        rows = D["rows"]
        given = [x["given"] for x in rows]
        b = _block(D["ticks"], scale, rate, unit=unit)
        night = D["night"]
        b.update({
            "day": d,
            "seen": len(rows),
            "processed": D["proc"],
            "approved": sum(g == "APPROVED" for g in given),
            "denied": sum(g == "DENIED" for g in given),
            "left": sum(g is None for g in given),
            "correct": D["n_ok"],
            "wrong": D["n_bad"],
            "citations": D["cits"],
            "night": night is not None,
            "savings": night.get("savings") if night else None,
        })
        days[d] = b
    tot = _block(ticks, scale, rate, sm["calls"] if (have_sm and full_run) else None, unit)
    tot["billed"] = sm["cost"] if (have_sm and full_run and sm.get("cost")) else None   # real API bill, if any
    for k in ("seen", "processed", "approved", "denied", "left", "correct", "wrong", "citations"):
        tot[k] = sum(days[d][k] for d in days)
    nights = [days[d] for d in days if days[d]["savings"] is not None]
    tot["savings"] = nights[-1]["savings"] if nights else None   # savings is a running balance: last night's value
    tot["pre_ticks"] = len(ticks) - sum(days[d]["ticks"] for d in days)
    last = ticks[-1] if ticks else {}
    g = last.get("gt") or {}
    cur = max(days) if days else None
    status = "starting"
    if cur is not None:
        status = "night" if days[cur]["night"] else "day"
    return {
        "run": os.path.basename(os.path.normpath(sm.get("_run_dir", ""))) if sm else "",
        "days": days, "tot": tot, "cur_day": cur, "status": status, "final_day": final_day,
        "rate_per_1k": rate, "unit": unit, "calls_exact": have_sm and full_run,
        "last_tick": last.get("tick"), "gt_ok": bool(g.get("ok")),
        "finished": have_sm and full_run,
    }


def run_summary(run_dir: str, ticks: list[dict]) -> dict | None:
    """summary.md numbers + the full-run apportioning scale, or None if the run hasn't finished."""
    if not os.path.exists(os.path.join(run_dir, "summary.md")):
        return None
    sm = R.summary_md(run_dir)
    if not sm.get("calls"):
        return None
    est = sum(R.tick_calls(r) for r in ticks) or 1
    sm["_scale"] = sm["calls"] / est
    sm["_run_dir"] = run_dir
    return sm


def tally_for(run_dir: str, ticks: list[dict], upto_tick: int | None = None, live: bool = False, **kw) -> dict:
    """Tally of a run (all ticks, or the prefix up to `upto_tick`).
    live=True: exactly what the live window rendered when tick `upto_tick` was the newest one loaded, i.e. before
    summary.md existed (request counts = the log's own count, not apportioned from summary.md)."""
    sub = ticks if upto_tick is None else [r for r in ticks if r["tick"] <= upto_tick]
    if live:
        T = compute(sub, None, full_run=True, **kw)
        T["run"] = os.path.basename(os.path.normpath(run_dir))
        return T
    sm = run_summary(run_dir, ticks)
    T = compute(sub, sm, full_run=(upto_tick is None or not sub or sub[-1] is ticks[-1]), **kw)
    T["run"] = os.path.basename(os.path.normpath(run_dir))
    return T


# ---------------------------------------------------------------- triggers
def day_end_events(ticks: list[dict], final_day: int = 3) -> list[dict]:
    """When the tally should pop, from the log alone: {kind: day_end|final, day, tick}.
    day_end: first tick where game memory (gt) shows the night screen of day d; if that was never seen, the first
    tick of day d+1. Without gt at all: TOD's own screen answer `day_end` (p >= 0.5) two ticks in a row.
    The day-end of `final_day` is kind=final (the final screen stays up)."""
    out, done = [], set()
    prev_day, streak, n_tod_ends = None, 0, 0
    for r in ticks:
        g = r.get("gt") or {}
        if g.get("ok") and isinstance(g.get("day"), int):
            d = g["day"]
            if prev_day is not None and d > prev_day and prev_day not in done:
                done.add(prev_day)
                out.append({"day": prev_day, "tick": r["tick"], "why": f"gt day {prev_day}->{d}"})
            prev_day = d
            if g.get("screen") == "NightScreen" and d not in done:
                done.add(d)
                out.append({"day": d, "tick": r["tick"], "why": "gt NightScreen"})
            continue
        s = ((r.get("state") or {}).get("screen") or {})
        if prev_day is None and s.get("value") == "day_end" and (s.get("p") or 0) >= 0.5:
            streak += 1
            if streak == 2:
                n_tod_ends += 1
                out.append({"day": n_tod_ends, "tick": r["tick"], "why": "TOD screen=day_end x2 (no gt)"})
        else:
            streak = 0
    for e in out:
        e["kind"] = "final" if e["day"] >= final_day else "day_end"
    return out


# ---------------------------------------------------------------- render
INK = (46, 44, 34)
PAPER = (231, 221, 192)
PAPER_DK = (214, 202, 168)
RULE = (170, 158, 120)
OLIVE = (86, 96, 58)
OLIVE_DK = (40, 44, 30)
BG = (33, 36, 27)
DIM = (120, 112, 86)
GREEN = (62, 112, 52)
RED = (160, 44, 36)
GOLD = (196, 170, 96)

DESIGN_W, DESIGN_H = 1920, 1080

COLS = [   # key, header, group, width (design px), fmt
    ("seen", "SEEN", "ENTRANTS", 84, "n"),
    ("processed", "PROC", "ENTRANTS", 84, "n"),
    ("approved", "APPR", "ENTRANTS", 84, "n"),
    ("denied", "DENY", "ENTRANTS", 84, "n"),
    ("left", "LEFT", "ENTRANTS", 76, "n"),
    ("correct", "RIGHT", "GROUND TRUTH", 100, "ok"),
    ("wrong", "WRONG", "GROUND TRUTH", 100, "bad"),
    ("citations", "CIT.", "GROUND TRUTH", 84, "bad"),
    ("savings", "SAVED", "GROUND TRUTH", 100, "money0"),
    ("answers", "ANSWERS", "TOD", 120, "n"),
    ("picks", "PICKS", "TOD", 104, "n"),
    ("calls", "REQ=IMG", "TOD", 116, "n"),
    ("cost", "COST", "TOD", 112, "money"),
    ("ticks", "TICKS", "TIME", 96, "n"),
    ("wall_s", "WALL", "TIME", 112, "min"),
    ("med_tick_ms", "TICK", "TIME", 96, "sec"),
]


def cost_label(T: dict) -> str:
    return f"cost @ ${T.get('rate_per_1k', RATE_PER_1K):.2f} / 1k {T.get('unit', COST_UNIT)}"


def _fmt(v, kind: str, approx: bool = False) -> str:
    if v is None:
        return "-"
    if kind == "money":
        return ("~" if approx else "") + f"${v:.2f}"
    if kind == "money0":
        return f"{v:.0f}"
    if kind == "min":
        return f"{v / 60:.1f}m"
    if kind == "sec":
        return f"{v / 1e3:.1f}s"
    return f"{v:,.0f}"


class TallyRenderer:
    """Renders a tally dict (compute()) at any size; layout is designed at 1920x1080 and scaled."""

    def render(self, T: dict, size: tuple[int, int] = (1920, 1080), title: str | None = None) -> Image.Image:
        W, H = size
        s = min(W / DESIGN_W, H / DESIGN_H)
        self.s = s
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        ox, oy = (W - DESIGN_W * s) / 2, (H - DESIGN_H * s) / 2   # letterbox to keep the design aspect
        self.ox, self.oy = ox, oy
        F = lambda name, px: _font(name, max(8, int(px * s)))   # noqa: E731
        self.f = {
            "title": F("bahnschrift.ttf", 58), "sub": F("bahnschrift.ttf", 26), "lab": F("bahnschrift.ttf", 21),
            "hero": F("consolab.ttf", 92), "heros": F("bahnschrift.ttf", 22), "cell": F("consola.ttf", 34),
            "cellb": F("consolab.ttf", 36), "day": F("bahnschrift.ttf", 32), "grp": F("bahnschrift.ttf", 18),
            "foot": F("bahnschrift.ttf", 24), "stamp": F("bahnschrift.ttf", 34),
        }
        X = lambda x: ox + x * s   # noqa: E731
        Y = lambda y: oy + y * s   # noqa: E731
        self.X, self.Y = X, Y

        # paper sheet
        d.rectangle([X(40), Y(30), X(1880), Y(1050)], fill=PAPER, outline=OLIVE_DK, width=max(1, int(4 * s)))
        d.rectangle([X(40), Y(30), X(1880), Y(150)], fill=OLIVE)
        tot, days = T["tot"], T["days"]
        final = T.get("kind") == "final" or (T.get("finished") and T.get("status") == "night")
        if title is None:
            if not days:
                title = "TALLY"
            elif final:
                lo, hi = min(days), max(days)
                title = f"FINAL TALLY  -  DAY {lo}" + (f"-{hi}" if hi != lo else "")
            elif T["status"] == "night":
                title = f"TALLY  -  END OF DAY {T['cur_day']}"
            else:
                title = f"TALLY  -  DAY {T['cur_day']} IN PROGRESS"
        d.text((X(80), Y(52)), title, font=self.f["title"], fill=PAPER)
        d.text((X(1840), Y(60)), "TOD plays Papers, Please", font=self.f["sub"], fill=PAPER_DK, anchor="ra")
        d.text((X(1840), Y(98)), f"run {T.get('run', '')}  ·  tick {T.get('last_tick', '-')}", font=self.f["lab"],
               fill=PAPER_DK, anchor="ra")

        # ---- hero numbers (cumulative)
        judged = tot["correct"] + tot["wrong"]
        heroes = [
            ("CORRECT", f"{tot['correct']}/{judged}" if judged else "-", GREEN,
             "stamped right  ·  ground truth" + (f"  ·  {tot['left']} left unstamped" if tot["left"] else "")),
            ("CITATIONS", f"{tot['citations']}", RED if tot["citations"] else INK, "issued by the game"),
            ("PROCESSED", f"{tot['processed']}", INK, f"{tot['seen']} entrants reached the booth"),
            ("TOD DECISIONS", f"{tot['answers']:,}", OLIVE_DK, f"questions answered  ·  {tot['picks']} picks executed"),
            ("TOD COST", _fmt(tot["cost"], "money"), OLIVE_DK,
             (cost_label(T), f"{tot['calls']:,.0f} requests = {tot['images']:,.0f} images")),
        ]
        hx, hw = 80, 352
        for i, (lab, val, col, sub) in enumerate(heroes):
            x0 = hx + i * hw
            d.rectangle([X(x0), Y(180), X(x0 + hw - 24), Y(400)], fill=PAPER_DK, outline=RULE, width=max(1, int(2 * s)))
            d.text((X(x0 + 20), Y(192)), lab, font=self.f["lab"], fill=DIM)
            d.text((X(x0 + 18), Y(222)), val, font=self._fit(val, "hero", hw - 60), fill=col)
            if isinstance(sub, tuple):   # explicit lines (cost tile: the rate label, then requests = images)
                for j, ln in enumerate(sub):
                    d.text((X(x0 + 20), Y(338) + j * self.f["heros"].size * 1.15), ln,
                           font=self._fit(ln, "heros", hw - 60), fill=INK)
            else:
                self._wrap(d, sub, X(x0 + 20), Y(338), (hw - 60) * s, self.f["heros"], INK)

        # ---- ledger table
        y_grp, y_head, y0, rh = 432, 462, 512, 66
        x_day = 80
        xs, x = [], 270
        for c in COLS:
            xs.append(x)
            x += c[3]
        x_end = x
        # group headers
        groups: list[tuple[str, int, int]] = []
        for (k, h, g, w, _), xx in zip(COLS, xs):
            if groups and groups[-1][0] == g:
                groups[-1] = (g, groups[-1][1], xx + w)
            else:
                groups.append((g, xx, xx + w))
        for g, a, b in groups:
            lab = "GROUND TRUTH (not shown to TOD)" if g == "GROUND TRUTH" else g
            d.line([X(a + 8), Y(y_grp + 24), X(b - 8), Y(y_grp + 24)], fill=OLIVE, width=max(1, int(2 * s)))
            d.text((X((a + b) / 2), Y(y_grp)), lab, font=self.f["grp"], fill=OLIVE, anchor="ma")
        for (k, h, g, w, _), xx in zip(COLS, xs):
            d.text((X(xx + w - 10), Y(y_head)), h, font=self.f["lab"], fill=DIM, anchor="ra")
        d.line([X(x_day), Y(y0 - 6), X(x_end), Y(y0 - 6)], fill=INK, width=max(1, int(3 * s)))

        lo = min(days) if days else 1
        hi = max(T["final_day"], max(days) if days else 1)
        row_days = list(range(lo, hi + 1))[-5:]
        y = y0
        for i, dd in enumerate(row_days):
            D = days.get(dd)
            if i % 2 == 0:
                d.rectangle([X(x_day), Y(y), X(x_end), Y(y + rh)], fill=(224, 213, 180))
            live = D is not None and dd == T["cur_day"] and not D["night"]
            lab = f"DAY {dd}"
            d.text((X(x_day + 14), Y(y + rh / 2 - (9 if live else 0))), lab, font=self.f["day"],
                   fill=INK if D else DIM, anchor="lm")
            if live:
                d.text((X(x_day + 16), Y(y + rh - 5)), "IN PROGRESS", font=self.f["grp"], fill=OLIVE, anchor="ld")
            for (k, h, g, w, kind), xx in zip(COLS, xs):
                v = D.get(k) if D else None
                txt = _fmt(v, kind) if D else "·"
                col = INK
                if kind == "ok" and v:
                    col = GREEN
                elif kind == "bad" and v:
                    col = RED
                elif not D:
                    col = DIM
                d.text((X(xx + w - 10), Y(y + rh / 2)), txt, font=self.f["cell"], fill=col, anchor="rm")
            y += rh
        # totals
        d.line([X(x_day), Y(y + 4), X(x_end), Y(y + 4)], fill=INK, width=max(1, int(2 * s)))
        d.line([X(x_day), Y(y + 10), X(x_end), Y(y + 10)], fill=INK, width=max(1, int(2 * s)))
        y += 14
        d.text((X(x_day + 14), Y(y + rh / 2)), "TOTAL", font=self.f["day"], fill=INK, anchor="lm")
        for (k, h, g, w, kind), xx in zip(COLS, xs):
            v = tot.get(k)
            col = GREEN if (kind == "ok" and v) else RED if (kind == "bad" and v) else INK
            d.text((X(xx + w - 10), Y(y + rh / 2)), _fmt(v, kind),
                   font=self.f["cellb"], fill=col, anchor="rm")
        y += rh

        # stamp, Papers Please style, once the final day is over
        # ... only when the gt night screen of the last day was observed (run 171251 stopped at the booth: RUN ENDED)
        if final:
            done = bool(days) and bool(days[max(days)].get("night"))
            self._stamp(img, ("DAYS COMPLETE" if len(days) > 1 else "DAY COMPLETE") if done else "RUN ENDED",
                        X(1640), Y(y + 105))

        # ---- notes + footer
        notes = []
        if tot.get("pre_ticks"):
            notes.append(f"TOTAL includes {tot['pre_ticks']} title/menu ticks before the first day")
        notes.append(f"{cost_label(T)} ({T['unit']} x {T['rate_per_1k'] / 1000:g})" +
                     (f"; billed (API) ${tot['billed']:.2f}" if tot.get("billed") else ""))
        notes.append(f"tokens: {tot['tokens']:,}" if tot.get("tokens") else "tokens: not logged per tick")
        notes.append("SAVED = savings on the night screen;  TICK = median tick")
        self._wrap(d, "   ·   ".join(notes), X(x_day), Y(y + 22), 1300 * s, self.f["grp"], DIM)
        d.rectangle([X(40), Y(968), X(1880), Y(1050)], fill=PAPER_DK)
        d.line([X(40), Y(968), X(1880), Y(968)], fill=RULE, width=max(1, int(2 * s)))
        d.text((X(80), Y(1009)), "HOW IT WORKS", font=self.f["lab"], fill=OLIVE, anchor="lm")
        d.text((X(260), Y(1009)), "every decision = one image + numbered options  ->  TOD picks",
               font=self.f["foot"], fill=INK, anchor="lm")
        d.text((X(1840), Y(1009)), "right / wrong / citations: game memory, never shown to TOD", font=self.f["grp"],
               fill=DIM, anchor="rm")
        return img

    def _fit(self, text, key, width_design):
        f = self.f[key]
        size = f.size
        while size > 10 and f.getlength(text) > width_design * self.s:
            size = int(size * 0.9)
            f = f.font_variant(size=size)
        return f

    def _wrap(self, d, text, x, y, width, font, fill, max_lines=2):
        words, line, lines = text.split(" "), "", []
        for w in words:
            t = (line + " " + w).strip()
            if font.getlength(t) <= width:
                line = t
            else:
                lines.append(line)
                line = w
        lines.append(line)
        for i, ln in enumerate(lines[:max_lines]):
            d.text((x, y + i * font.size * 1.15), ln.strip("· "), font=font, fill=fill)

    def _stamp(self, img, text, cx, cy):
        f = self.f["stamp"]
        tw = f.getlength(text)
        pad = 18 * self.s
        w, h = int(tw + 2 * pad), int(f.size * 1.9)
        st_img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        sd = ImageDraw.Draw(st_img)
        lw = max(2, int(5 * self.s))
        sd.rectangle([lw, lw, w - lw, h - lw], outline=GREEN + (215,), width=lw)
        sd.text((w / 2, h / 2), text, font=f, fill=GREEN + (215,), anchor="mm")
        st_img = st_img.rotate(6, expand=True, resample=Image.BICUBIC)
        img.paste(st_img, (int(cx - st_img.width / 2), int(cy - st_img.height / 2)), st_img)


def numbers_text(T: dict) -> str:
    """Plain-text numbers, phrased like tools/report.py's totals lines (for checking the two agree)."""
    out = []
    for d, D in sorted(T["days"].items()):
        out.append(f"Day {d}: seen {D['seen']}, processed {D['processed']}, approved {D['approved']}, denied "
                   f"{D['denied']}, left {D['left']}, correct {D['correct']} / wrong {D['wrong']}, citations "
                   f"{D['citations']}, savings {D['savings'] if D['savings'] is not None else '-'}, ticks {D['ticks']}, "
                   f"decisions {D['answers']}, picks {D['picks']} {D['picks_by']}, TOD calls ~{D['calls']:.0f}, "
                   f"images ~{D['images']:.0f}, ${D['cost']:.4f}, wall {D['wall_s'] / 60:.1f} min, median tick "
                   f"{D['med_tick_ms'] or 0:.0f} ms")
    t = T["tot"]
    out.append(f"Total: seen {t['seen']}, processed {t['processed']}, approved {t['approved']}, denied {t['denied']}, "
               f"left {t['left']}, correct {t['correct']} / wrong {t['wrong']}, citations {t['citations']}, savings "
               f"{t['savings'] if t['savings'] is not None else '-'}, ticks {t['ticks']} (pre-day {t['pre_ticks']}), "
               f"decisions {t['answers']}, picks {t['picks']} {t['picks_by']}, TOD calls {t['calls']:.0f}"
               f"{'' if T['calls_exact'] else ' (log count)'}, images {t['images']:.0f}, ${t['cost']:.4f}, tokens "
               f"{t['tokens'] if t['tokens'] else 'not logged'}, wall-clock {t['wall_s'] / 60:.1f} min, median tick "
               f"{t['med_tick_ms'] or 0:.0f} ms")
    out.append(f"{cost_label(T)}: cost = {T['unit']} x {T['rate_per_1k'] / 1000:g}" +
               (f"; billed (API, summary.md) ${t['billed']:.4f}" if t.get("billed") else ""))
    return "\n".join(out)


def now_iso(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() if t is None else t))
