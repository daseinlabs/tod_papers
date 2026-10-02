r"""Compare extract_remote() against local extract() on saved frames.

    .venv-loop\Scripts\python tools\check_remote_extract.py http://127.0.0.1:8765 FRAME.png [...]
         [--local]   # also run local extract() in-process (needs the GPU stack here)

Prints per-frame equality (auto/lossless-native and jpeg q90) and round-trip latency.
"""
from __future__ import annotations

import os
import statistics as st
import sys

import cv2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("TOD_EXTRACT_VERBOSE", "0")
from tod_papers import extract as ex  # noqa: E402
from tod_papers import extract_remote as er  # noqa: E402


def key(b):
    return (b.x1, b.y1, b.x2, b.y2, b.kind, b.text, b.caption, b.parent, round(b.conf, 3))


def diff(a, b) -> str:
    ka, kb = {key(x) for x in a}, {key(x) for x in b}
    return f"only_local={len(ka - kb)} only_remote={len(kb - ka)}"


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    url, frames = args[0], args[1:]
    local = "--local" in sys.argv
    if local:
        ex.warmup()
    os.environ["TOD_EXTRACT_URL"] = url
    er.warmup()
    for p in frames:
        f = cv2.imread(p)
        print(f"\n== {p} {f.shape[1]}x{f.shape[0]}")
        ref = None
        if local:
            ref = ex.extract(f)
            ref2 = ex.extract(f)
            print(f"local extract: {len(ref)} boxes, {ex.LAST_TIMINGS['total_ms']:.0f} ms; "
                  f"local run-to-run identical={[key(b) for b in ref] == [key(b) for b in ref2]}")
        for fmt in ("auto", "jpeg"):
            tot, srv, net, enc = [], [], [], []
            for i in range(5):
                bs = er.extract_remote(f, url, fmt=fmt)
                T = ex.LAST_TIMINGS
                tot.append(T["total_ms"]); srv.append(T["srv_server_ms"]); net.append(T["net_ms"]); enc.append(T["encode_ms"])
            line = (f"remote[{fmt:4s}] {int(T['bytes'])/1024:6.1f} KB  boxes={len(bs)}  "
                    f"round-trip median {st.median(tot):.0f} ms (encode {st.median(enc):.0f}, server {st.median(srv):.0f}, "
                    f"net+http {st.median(net):.0f})")
            if ref is not None:
                same = [key(b) for b in ref] == [key(b) for b in bs]
                same_desc = [ex.describe(b) for b in ref] == [ex.describe(b) for b in bs]
                line += f"  identical={same} describe_identical={same_desc} {'' if same else diff(ref, bs)}"
            print(line)


if __name__ == "__main__":
    main()
