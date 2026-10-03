"""Ground-truth probe for a running Papers, Please (read-only memory reads).

    python tools/gt_probe.py            # one JSON snapshot
    python tools/gt_probe.py --watch    # one line per change, polled at --hz (default 10)
    python tools/gt_probe.py --watch --jsonl runs/gt.jsonl   # also append full snapshots on change

Labels/eval only -- never feed these values to TOD.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from tod_papers import gt  # noqa: E402


def _key(s: dict) -> tuple:
    e = s.get("entrant") or {}
    return (
        s.get("ok"), s.get("error"), s.get("screen"), s.get("day"), s.get("clock"),
        s.get("day_processed"), s.get("day_made"), s.get("num_citations"),
        tuple(c["type"] for c in s.get("citations") or ()), s.get("savings"),
        s.get("stat_processed"), e.get("name"), e.get("correct_verdict"), e.get("given_verdict"),
        e.get("detained"), s.get("eng_have_stamped"),
    )


def _line(s: dict) -> str:
    ts = time.strftime("%H:%M:%S", time.localtime(s["t"])) + f".{int(s['t'] % 1 * 1000):03d}"
    if not s.get("ok"):
        return f"{ts} ERR {s.get('error')}"
    e = s.get("entrant") or {}
    cits = ",".join(c["type"][0] if c["type"] != "LASTWARNING" else "L" for c in s.get("citations") or []) or "-"
    ent = (f" entrant={e.get('name')!r} correct={e.get('correct_verdict')} given={e.get('given_verdict')}"
           f" errs={e.get('noticeable_errors')}" if e else "")
    return (f"{ts} {s.get('screen')} day={s.get('day')} date={s.get('date')} clock={s.get('clock')} "
            f"processed={s.get('day_processed')} made={s.get('day_made')} "
            f"citations={s.get('num_citations')}[{cits}] penalty={s.get('penalty_cost')} "
            f"savings={s.get('savings')} lifetime_processed={s.get('stat_processed')}{ent}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--hz", type=float, default=10.0)
    ap.add_argument("--jsonl", help="append full snapshot on each change")
    ap.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = forever)")
    a = ap.parse_args()
    if not a.watch:
        print(json.dumps(gt.snapshot(), indent=1, default=str))
        return 0
    period = 1.0 / a.hz
    last = None
    t_end = time.time() + a.duration if a.duration else None
    fh = open(a.jsonl, "a", encoding="utf8") if a.jsonl else None
    try:
        while t_end is None or time.time() < t_end:
            t0 = time.time()
            s = gt.snapshot()
            k = _key(s)
            if k != last:
                print(_line(s), flush=True)
                if fh:
                    fh.write(json.dumps(s, default=str) + "\n")
                    fh.flush()
                last = k
            time.sleep(max(0.0, period - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        if fh:
            fh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
