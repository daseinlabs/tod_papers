"""extract_remote.py -- client for extract_server: same contract as extract.extract().

    from tod_papers.extract_remote import extract_remote
    boxes = extract_remote(frame_bgr, "http://<host>:8765")   # -> list[extract.Box]

`extract(frame_bgr)` is the drop-in twin of `extract.extract(frame_bgr)` and
reads the URL from env TOD_EXTRACT_URL. After every call it also updates
`extract.LAST_TIMINGS` (server stages + client-side encode/rtt/parse) and
`extract._LAST_WH`, so `extract.describe(box)` and loop.py's timing logs work
unchanged.

Wire format (env TOD_EXTRACT_FMT, default "auto"):
  * auto  -- if the frame is exactly 4x pixel-replicated (always true for
             Papers, Please captures; checked per frame, a few ms) send the
             native 570x320 frame as lossless PNG (~20-25 KB) with scale=4, so the
             server reconstructs a bit-identical frame and the boxes equal a
             local extract(). Otherwise fall back to jpeg.
  * jpeg  -- full frame JPEG, quality TOD_EXTRACT_JPEG_Q (default 90), ~250 KB.
             Lossy: OCR / detector scores shift slightly vs local extract().
  * png   -- full frame lossless PNG (~280 KB, slower to encode).
Auth: env TOD_EXTRACT_TOKEN -> `Authorization: Bearer ...`.
"""
from __future__ import annotations

import dataclasses
import os
import time

import cv2
import numpy as np
import requests

from . import extract as _ex
from .extract import Box

_BOX_FIELDS = {f.name for f in dataclasses.fields(Box)}
# captured at import, before loop.py swaps ex.extract for the remote one (else the fallback would recurse)
_LOCAL_EXTRACT = _ex.extract
_LOCAL_WARMUP = _ex.warmup
FALLBACK_LOG: list = []          # [(time, error)] every local fallback, for the run log
_local_warm = False
_session = requests.Session()
TIMEOUT = float(os.environ.get("TOD_EXTRACT_TIMEOUT", "30"))
CONNECT_TIMEOUT = float(os.environ.get("TOD_EXTRACT_CONNECT_TIMEOUT", "3"))
LAST_RESPONSE: dict = {}


def _replicated(frame: np.ndarray, s: int) -> np.ndarray | None:
    """Native frame if `frame` is exactly s x s pixel-replicated, else None."""
    H, W = frame.shape[:2]
    if s < 2 or H % s or W % s:
        return None
    native = np.ascontiguousarray(frame[::s, ::s])
    rows = frame[::s]                       # every s-th row; then each column phase
    for dx in range(1, s):
        if not np.array_equal(rows[:, dx::s], native):
            return None
    for dy in range(1, s):                  # remaining rows must equal the sampled rows
        if not np.array_equal(frame[dy::s], rows):
            return None
    return native


def encode(frame_bgr: np.ndarray, fmt: str | None = None) -> tuple[bytes, str, int]:
    """-> (payload, mime, scale)."""
    fmt = (fmt or os.environ.get("TOD_EXTRACT_FMT", "auto")).lower()
    if fmt == "auto":
        s = int(round(frame_bgr.shape[1] / _ex.NATIVE_W))
        native = _replicated(frame_bgr, s)
        if native is not None:
            ok, b = cv2.imencode(".png", native, [cv2.IMWRITE_PNG_COMPRESSION, 1])
            return b.tobytes(), "image/png", s
        fmt = "jpeg"
    if fmt == "png":
        ok, b = cv2.imencode(".png", frame_bgr, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        return b.tobytes(), "image/png", 1
    q = int(os.environ.get("TOD_EXTRACT_JPEG_Q", "90"))
    ok, b = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, q])
    return b.tobytes(), "image/jpeg", 1


def extract_remote(frame_bgr: np.ndarray, url: str | None = None, fmt: str | None = None) -> list[Box]:
    url = (url or os.environ.get("TOD_EXTRACT_URL", "")).rstrip("/")
    if not url:
        raise RuntimeError("extract_remote: no url and TOD_EXTRACT_URL unset")
    t0 = time.perf_counter()
    payload, mime, scale = encode(frame_bgr, fmt)
    t1 = time.perf_counter()
    headers = {"Content-Type": mime}
    tok = os.environ.get("TOD_EXTRACT_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    r = _session.post(f"{url}/extract", params={"scale": scale}, data=payload, headers=headers,
                      timeout=(CONNECT_TIMEOUT, TIMEOUT))
    r.raise_for_status()
    t2 = time.perf_counter()
    js = r.json()
    boxes = [Box(**{k: v for k, v in d.items() if k in _BOX_FIELDS}) for d in js["boxes"]]
    t3 = time.perf_counter()
    H, W = frame_bgr.shape[:2]
    _ex._LAST_WH = (W, H)
    T = {f"srv_{k}": v for k, v in js.get("timings", {}).items()}
    rtt = (t2 - t1) * 1e3
    T.update({
        "encode_ms": (t1 - t0) * 1e3, "rtt_ms": rtt,
        "net_ms": rtt - js.get("timings", {}).get("server_ms", 0.0),   # wire + HTTP overhead
        "parse_ms": (t3 - t2) * 1e3, "total_ms": (t3 - t0) * 1e3,
        "bytes": float(len(payload)),
    })
    _ex.LAST_TIMINGS.clear()
    _ex.LAST_TIMINGS.update(T)
    LAST_RESPONSE.clear()
    LAST_RESPONSE.update(js)
    if _ex.VERBOSE:
        print(f"[extract_remote] {mime} {len(payload)/1024:.0f}KB encode={T['encode_ms']:.0f} "
              f"server={T.get('srv_server_ms', 0):.0f} net={T['net_ms']:.0f} total={T['total_ms']:.0f} ms "
              f"boxes={len(boxes)}")
    return boxes


def extract(frame_bgr: np.ndarray) -> list[Box]:
    """Same signature as extract.extract(); URL from TOD_EXTRACT_URL.
    If the remote server fails (tunnel down, VM preempted, HTTP error, timeout) this tick falls back to the local
    extract.extract() and logs it (FALLBACK_LOG, LAST_TIMINGS['fallback_local']=1). The next tick tries the
    remote server again."""
    global _local_warm
    try:
        try:
            return extract_remote(frame_bgr)
        except requests.ConnectionError as e:   # stale keep-alive socket (run 070005 t7: RemoteDisconnected ->
            print(f"[extract_remote] retrying once ({type(e).__name__})")   # 47 s cold local fallback)
            return extract_remote(frame_bgr)
    except (requests.RequestException, ValueError, KeyError) as e:
        err = f"{type(e).__name__}: {str(e)[:200]}"
        FALLBACK_LOG.append((time.time(), err))
        print(f"[extract_remote] REMOTE FAILED ({err}); falling back to local extract()"
              + ("" if _local_warm else " (first local call loads the models; slow)"))
        t0 = time.perf_counter()
        boxes = _LOCAL_EXTRACT(frame_bgr)
        _local_warm = True
        T = dict(_ex.LAST_TIMINGS)
        T.update({"fallback_local": 1.0, "fallback_ms": (time.perf_counter() - t0) * 1e3})
        _ex.LAST_TIMINGS.clear()
        _ex.LAST_TIMINGS.update(T)
        return boxes


def warmup() -> None:
    """Server warms its own models at startup; this just checks it is up and opens the connection.
    If it is down, warm the local models instead so the fallback is ready."""
    global _local_warm
    url = os.environ.get("TOD_EXTRACT_URL", "").rstrip("/")
    try:
        h = _session.get(f"{url}/health", timeout=(CONNECT_TIMEOUT, TIMEOUT)).json()
        print(f"[extract_remote] {url}: warm={h.get('warm')} gpu={h.get('gpu')} ocr={h.get('ocr_engine')}")
    except (requests.RequestException, ValueError) as e:
        print(f"[extract_remote] {url} unreachable ({type(e).__name__}); warming the local extractor for fallback")
        FALLBACK_LOG.append((time.time(), f"warmup: {type(e).__name__}"))
        _LOCAL_WARMUP()
        _local_warm = True
