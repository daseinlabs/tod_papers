"""report.py -- per-day / per-entrant summary of one loop run, from its tick JSONs.

    .venv-loop\\Scripts\\python.exe tools\\report.py [--rate-per-1k 0.30] [--cost-unit decisions|requests] runs\\<run_dir>

Sources (all already in the run folder, nothing is re-asked):
  - `gt` (game memory, eval only): day, screen, entrant name, correct / given verdict, day_processed,
    num_citations, savings. gt is only used here and for the harness stop; it is never in any TOD text.
  - TOD's own verdict answer (request 1b, `entrant.verdict` = {value, p, tick}) at the tick of the first stamp press,
    and the stamp side(s) TOD pressed (`entrant.stamp_clicks`).
  - timings: extract_ms, state_ms (request 1), doc_ms (request 1b, parallel to 1), tod_ms (request 2).
  - summary.md: total TOD calls (per-day calls are apportioned by the requests each tick made) and the API bill,
    shown only as a secondary "billed" line.
  - cost = $0.30 per 1,000 decisions (decision = one question TOD answered; --rate-per-1k), per day and total,
    the same whether TOD ran on the API or on our own GPU box.
"""
from __future__ import annotations

import glob
import json
import os
import re
import statistics as st
import sys


def load(run_dir: str) -> list[dict]:
    out = []
    for f in sorted(glob.glob(os.path.join(run_dir, "tick_*.json"))):
        with open(f, encoding="utf-8") as fh:
            out.append(json.load(fh))
    return out


def summary_md(run_dir: str) -> dict:
    p = os.path.join(run_dir, "summary.md")
    s = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
    calls = re.search(r"- TOD calls: (\d+)", s)
    cost = re.search(r"- cost: \$([\d.]+)", s)
    args = re.search(r"- args: (.*)", s)
    stop = re.search(r"- stop reason: (.*)", s)
    return {"calls": int(calls.group(1)) if calls else None, "cost": float(cost.group(1)) if cost else None,
            "args": args.group(1) if args else "", "stop": stop.group(1) if stop else "-"}


def tick_calls(r: dict) -> int:
    """Requests this tick made: request 1 (state), 1b (documents, when it ran), request 2 (action)."""
    return int(bool(r.get("state_ms"))) + int((r.get("doc_ms") or 0) > 50) + int(bool(r.get("tod_ms")))


RATE_PER_1K = 0.30   # $ per 1,000 billing units; docs/tally.md "Cost model"
# billing unit: "decisions" = questions TOD answered (tod-api /v1/systemone books decisions=len(questions) per
# request), or "requests" = TOD calls
COST_UNIT = "decisions"
UNITS = ("decisions", "requests")


def tick_answers(r: dict) -> int:
    """Questions TOD answered this tick (= decisions): request-1/1b state answers + request-2 answers, each one
    carrying its probabilities."""
    vals = list((r.get("state") or {}).values()) + list((r.get("answers") or {}).values())
    return sum(isinstance(v, dict) and ("probs" in v or "probabilities" in v) for v in vals)


def decision_cost(answers: float, rate_per_1k: float = RATE_PER_1K) -> float:
    return rate_per_1k * answers / 1000.0


def med(xs) -> str:
    xs = [x for x in xs if isinstance(x, (int, float)) and x > 0]
    return f"{st.median(xs):.0f}" if xs else "-"


def table(rows: list[list], head: list[str]) -> str:
    cells = [head] + [[str(c) for c in r] for r in rows]
    w = [max(len(r[i]) for r in cells) for i in range(len(head))]
    line = lambda r: "  ".join(c.ljust(w[i]) for i, c in enumerate(r)).rstrip()
    return "\n".join([line(head), line(["-" * x for x in w])] + [line(r) for r in cells[1:]])


def analyze(ticks: list[dict], sm: dict, rate_per_1k: float = RATE_PER_1K, unit: str = COST_UNIT) -> dict:
    """Per-day numbers of one run (shared with the tally screen, src/tod_papers/tally.py, so both always agree).
    Works on a partial run too (the loop still writing): days not yet at night have night=None.
    cost = rate_per_1k * <unit> / 1000 (decisions = questions TOD answered, or requests), per day and total, whatever
    served TOD (API or our own GPU box); a real API bill from summary.md is reported separately as `billed`."""
    est = sum(tick_calls(r) for r in ticks) or 1
    scale = (sm["calls"] / est) if sm["calls"] else 1.0

    # ---- group ticks: day -> entrant segments (consecutive ticks with the same gt entrant name) ----------------
    days: dict[int, dict] = {}
    cur_day = None
    for r in ticks:
        g = r.get("gt") or {}
        if g.get("ok") and isinstance(g.get("day"), int):
            cur_day = g["day"]
        if cur_day is None:
            continue   # menu ticks before the first day reading
        D = days.setdefault(cur_day, {"ticks": [], "segs": [], "night": None})
        D["ticks"].append(r)
        if not g.get("ok"):
            continue
        if g.get("screen") == "NightScreen" and (D["night"] is None or g.get("savings") is not None):
            D["night"] = g   # savings is written by the night screen a few ticks in: keep the last reading
        name = g.get("entrant") if g.get("screen") == "DayScreen" else None
        if name:
            if not D["segs"] or D["segs"][-1]["name"] != name:
                D["segs"].append({"name": name, "ticks": []})
            D["segs"][-1]["ticks"].append(r)

    by_tick = {r["tick"]: r for r in ticks}
    for d in sorted(days):
        D = days[d]
        rows = []
        n_ok = n_bad = 0
        for i, s in enumerate(D["segs"]):
            correct = given = None
            press = None   # (tick, verdict dict at that tick)
            stamps: list[str] = []
            for r in s["ticks"]:
                g, e = r["gt"], r.get("entrant") or {}
                correct = g.get("correct") or correct
                given = g.get("given") or given
                for t, side in e.get("stamp_clicks") or []:
                    if press is None:
                        press = t
                    if not stamps or stamps[-1] != side:
                        stamps.append(side)
            if press is not None:   # TOD's request-1b verdict answer logged on the press tick
                pr = by_tick.get(press)
                v = (pr or {}).get("entrant", {}).get("verdict") or {}
                verdict = f"{(v.get('value') or '-').upper()} {v.get('p', 0):.2f}" if v else "-"
            else:
                verdict = "-"
            if verdict == "-" and stamps:
                verdict = "(not logged)"   # runs before the request-1b verdict question (loop15) have no answer
            # citation: num_citations grows once the entrant has left (seen on the next entrant / the night screen)
            c0 = s["ticks"][0]["gt"].get("num_citations") or 0
            nxt = D["segs"][i + 1]["ticks"][0]["gt"] if i + 1 < len(D["segs"]) else (D["night"] or s["ticks"][-1]["gt"])
            cit = (nxt.get("num_citations") or 0) > c0
            if given is None:
                ok = "no stamp"
            else:
                ok = "yes" if given == correct else "NO"
                n_ok += given == correct
                n_bad += given != correct
            rows.append({"name": s["name"], "verdict": verdict, "stamps": stamps, "correct": correct, "given": given,
                         "ok": ok, "ticks": len(s["ticks"]), "citation": cit})
        gts = [r["gt"] for r in D["ticks"] if (r.get("gt") or {}).get("ok") and r["gt"].get("day") == d]
        D["rows"], D["n_ok"], D["n_bad"] = rows, n_ok, n_bad
        D["proc"] = max((g.get("day_processed") or 0 for g in gts), default=0)
        D["cits"] = max((g.get("num_citations") or 0 for g in gts), default=0)
        D["calls"] = sum(tick_calls(r) for r in D["ticks"]) * scale
        D["answers"] = sum(tick_answers(r) for r in D["ticks"])
        D["cost"] = decision_cost(D["answers"] if unit == "decisions" else D["calls"], rate_per_1k)
    answers = sum(tick_answers(r) for r in ticks)
    pre = answers - sum(D["answers"] for D in days.values())   # title/menu ticks before the first day reading
    calls = sm["calls"] or est
    return {"days": days, "scale": scale, "rate_per_1k": rate_per_1k, "unit": unit, "answers": answers,
            "pre_answers": pre, "cost": decision_cost(answers if unit == "decisions" else calls, rate_per_1k),
            "billed": sm["cost"] if sm.get("cost") else None}   # real API bill (summary.md), None if $0 / absent


def report(run_dir: str, rate_per_1k: float = RATE_PER_1K, unit: str = COST_UNIT) -> str:
    ticks = load(run_dir)
    if not ticks:
        return f"{run_dir}: no tick JSONs"
    sm = summary_md(run_dir)
    paused = "--pause-think" in sm["args"] or any((r.get("suspend") or {}).get("suspended") for r in ticks)
    A = analyze(ticks, sm, rate_per_1k, unit)
    days = A["days"]

    out = [f"Run {os.path.basename(os.path.normpath(run_dir))}  (stop: {sm['stop']})"]
    tot = {"proc": 0, "ok": 0, "bad": 0, "cit": 0}
    for d in sorted(days):
        D = days[d]
        rows = [[x["name"], x["verdict"], "/".join(s.upper()[0] for s in x["stamps"]) or "-", x["correct"] or "-",
                 x["given"] or "-", x["ok"], x["ticks"], "CITATION" if x["citation"] else ""] for x in D["rows"]]
        proc, cits, n_ok, n_bad, calls, cost = D["proc"], D["cits"], D["n_ok"], D["n_bad"], D["calls"], D["cost"]
        out.append(f"\nDay {d}")
        out.append(table(rows, ["entrant", "TOD verdict@press", "stamp", "gt correct", "given", "ok", "ticks",
                                "citation"]) if rows else "(no entrant seen)")
        night = D["night"]
        out.append(f"Day {d} totals: entrants processed {proc}, correct {n_ok} / wrong {n_bad}, citations {cits}, "
                   f"savings at night {night.get('savings') if night else '- (night not reached)'}, "
                   f"ticks {len(D['ticks'])}, TOD calls ~{calls:.0f}, decisions {D['answers']}, ${cost:.4f}")
        tot["proc"] += proc
        tot["ok"] += n_ok
        tot["bad"] += n_bad
        tot["cit"] += cits


    t0, t1 = ticks[0]["time"], ticks[-1]["time"]
    dt = [b["time"] - a["time"] for a, b in zip(ticks, ticks[1:])]
    out.append(f"\nRun totals: days {min(days)}-{max(days)}, entrants processed {tot['proc']}, correct {tot['ok']} / "
               f"wrong {tot['bad']}, citations {tot['cit']}, ticks {len(ticks)}, TOD calls {sm['calls']}, "
               f"decisions {A['answers']}" + (f" (incl. {A['pre_answers']} pre-day)" if A["pre_answers"] else "") +
               f", ${A['cost']:.4f}")
    out.append(f"cost @ ${rate_per_1k:.2f} / 1k {unit} ({unit} x {rate_per_1k / 1000:g})" +
               (f"; billed (API, summary.md) ${A['billed']:.4f}" if A["billed"] else ""))
    out.append(f"wall-clock {(t1 - t0) / 60:.1f} min; median tick {1e3 * st.median(dt):.0f} ms" if dt else "")
    out.append(f"median ms: extract {med(r.get('extract_ms') for r in ticks)}, req1 {med(r.get('state_ms') for r in ticks)}"
               f" (1b {med(r.get('doc_ms') for r in ticks)}, parallel), req2 {med(r.get('tod_ms') for r in ticks)}; "
               f"pause-think {'ON' if paused else 'OFF'}")
    return "\n".join(x for x in out if x != "")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    rate = RATE_PER_1K
    if "--rate-per-1k" in argv:
        i = argv.index("--rate-per-1k")
        rate = float(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    unit = COST_UNIT
    if "--cost-unit" in argv:
        i = argv.index("--cost-unit")
        unit = argv[i + 1]
        assert unit in UNITS, f"--cost-unit {unit}: one of {UNITS}"
        argv = argv[:i] + argv[i + 2:]
    if not argv:
        print(__doc__)
        return 1
    for rd in argv:
        print(report(rd, rate, unit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
