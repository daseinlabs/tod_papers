r"""fast_replay.py -- replay reconstructed /v1/systemone requests from a run against two TOD endpoints (A/B).

    .venv-loop\Scripts\python.exe tools\fast_replay.py --run runs\20261004_115735 --n 5 \
        --a https://tod.parseclab.ai --b http://127.0.0.1:8790

Requests are rebuilt from the tick logs exactly the way the loop builds them (the same reconstruction as the
shared-prefix benchmark): request 1 = loop.state_probe on raw_NNNN.png (every state question the manual asks for
that screen family); request 2 = tick state_text + the annotated tick_NNNN.png + loop.build_questions over the
logged label sets. Each request is sent once to A and once to B (B warmed by one throwaway call); prints per-request
client wall latency and the max |dp| over all options of all questions. TOD_API_KEY (env/.env) is used for the
public API only; no key is needed for a localhost URL.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

import cv2  # noqa: E402
from tod_papers import loop  # noqa: E402
from tod_papers import tod_client as tc  # noqa: E402

LARGS = types.SimpleNamespace(send_width=1140, send_format="png", jpeg_quality=90)


class Capture:
    def ask(self, questions, text=None, image_bgr=None, image_data_url=None):
        self.q, self.text, self.url = questions, text, image_data_url
        raise StopIteration


def requests_from_run(run: str):
    for f in sorted(glob.glob(os.path.join(run, "tick_*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        t = d.get("tick", int(os.path.basename(f)[5:9]))
        raw, ann = os.path.join(run, f"raw_{t:04d}.png"), os.path.join(run, f"tick_{t:04d}.png")
        if not (os.path.exists(raw) and os.path.exists(ann) and d.get("answers")):
            continue
        cap = Capture()
        try:
            loop.state_probe(cap, cv2.imread(raw), LARGS, str((d.get("gt") or {}).get("day") or "3"),
                             tuple(d.get("inspect_asked") or loop.man.INSPECT_KEYS), {}, False,
                             tuple(d["screen_family"]) if d.get("screen_family") else None, None, None)
        except StopIteration:
            pass
        yield f"t{t:04d}_r1", cap.q, cap.text, cap.url
        a, desc = d["answers"], d.get("descriptions") or {}
        src = {k: desc.get(k, k) for k in a["source"]["probabilities"] if k != loop.WAIT_KEY}
        tgt = {k: desc.get(k, k) for k in a.get("target", {}).get("probabilities", {})}
        q2 = loop.build_questions(src, tgt, len(tgt) >= 2, "")
        if len(tgt) < 2:
            q2.pop("target", None)
        url = loop.encode_image(loop._small_for_send(cv2.imread(ann), LARGS), "png", 90)
        yield f"t{t:04d}_r2", q2, d["state_text"], url


def client(base: str) -> tc.TodClient:
    os.environ["TOD_API_URL"] = base
    return tc.TodClient(timeout=120, retries=1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", default=os.path.join(ROOT, "runs", "20261004_115735"))
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--r1-min-q", type=int, default=9, help="request 1s with fewer questions are skipped")
    ap.add_argument("--a", default=tc.TOD_API_BASE, help="reference endpoint (default: public API)")
    ap.add_argument("--b", default="http://127.0.0.1:8790", help="candidate endpoint (default: tod-fast tunnel)")
    ap.add_argument("--out", default=None, help="write per-request rows as JSON here")
    args = ap.parse_args(argv)
    ca, cb = client(args.a), client(args.b)
    # request 1s with >= --r1-min-q questions, and the booth request 2 (3 questions) of the same ticks: ~60/40
    n2 = max(1, round(args.n * 0.4))
    picked, k1, k2, booth = [], 0, 0, False
    for rid, q, text, url in requests_from_run(args.run):
        if rid.endswith("_r1"):
            booth = len(q) >= args.r1_min_q          # a booth tick: its request 2 qualifies too
            if booth and k1 < args.n - n2:
                k1 += 1
                picked.append((rid, q, text, url))
        elif booth and len(q) >= 3 and k2 < n2:
            k2 += 1
            picked.append((rid, q, text, url))
        if k1 + k2 >= args.n:
            break
    cb.ask(*picked[0][1:2], text=picked[0][2], image_data_url=picked[0][3])   # warm B (first call pays setup)
    rows = []
    print(f"{'request':<12}{'Q':>3}{'opts':>5}{'A ms':>8}{'B ms':>8}{'A/B':>6}{'max|dp|':>9}  argmax same")
    for rid, q, text, url in picked:
        ra = ca.ask(q, text=text, image_data_url=url)
        rb = cb.ask(q, text=text, image_data_url=url)
        dp, same, nopt = 0.0, 0, 0
        for k, a in ra.answers.items():
            b = rb.answers[k]
            nopt += len(a.probabilities)
            dp = max([dp] + [abs(a.probabilities[o] - b.probabilities.get(o, 0.0)) for o in a.probabilities])
            same += a.top == b.top
        row = dict(id=rid, q=len(q), opts=nopt, a_ms=round(ra.latency_ms), b_ms=round(rb.latency_ms),
                   speedup=round(ra.latency_ms / rb.latency_ms, 2), max_abs_dp=round(dp, 4), argmax_same=same,
                   b_picker_ms=rb.raw.get("picker_ms"))
        rows.append(row)
        print(f"{rid:<12}{row['q']:>3}{nopt:>5}{row['a_ms']:>8}{row['b_ms']:>8}{row['speedup']:>6}{dp:>9.4f}  "
              f"{same}/{len(q)}")
    print(f"max |dp| over all {sum(r['opts'] for r in rows)} options: {max(r['max_abs_dp'] for r in rows):.4f}")
    if args.out:
        json.dump(rows, open(args.out, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
