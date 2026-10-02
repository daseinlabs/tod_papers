"""extract_server.py -- the extract() pipeline as an HTTP service.

Runs the exact same `tod_papers.extract.extract()` (YOLO icon_detect + Grounding
DINO + PP-OCRv5 + CLIP) on whatever box hosts it (a cloud L4, or this laptop for
testing), so the game machine only has to ship the frame. Client:
`tod_papers.extract_remote` (see docs/remote_extraction.md).

    python -m tod_papers.extract_server --host 0.0.0.0 --port 8765

Endpoints
---------
GET  /health   -> {"ok", "warm", "cuda", "gpu", "ocr_engine", "labels", ...}
POST /extract  -> {"W", "H", "boxes": [Box fields + "describe"], "timings": {...}}

POST /extract accepts the image three ways (PNG, JPEG or WebP bytes):
  * multipart/form-data, file field `image`
  * application/json  {"image_b64": "<base64>", "scale": 1}
  * raw body with Content-Type image/png | image/jpeg | image/webp
Query/JSON param `scale` (int, default 1): the image is the native-resolution
frame and is upscaled `scale`x nearest-neighbour before extract(). Papers,
Please renders 570x320 art at exactly 4x, so a 4x-downsampled frame is
lossless and the upscaled copy is bit-identical to the original capture
(the client checks this before using it).

Auth: if env TOD_EXTRACT_TOKEN is set, requests must carry
`Authorization: Bearer <token>` (/health is open).

extract() keeps module-level state (lazy models, LAST_TIMINGS, the OCR worker
pool), so calls are serialised with a lock; one GPU box serves one loop.
"""
from __future__ import annotations

import argparse
import base64
import os
import threading
import time
from contextlib import asynccontextmanager

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request

from . import extract as ex

_lock = threading.Lock()
_state: dict = {"warm": False, "warm_ms": None, "calls": 0}
TOKEN = os.environ.get("TOD_EXTRACT_TOKEN", "")


def _gpu_info() -> dict:
    try:
        import torch

        if torch.cuda.is_available():
            return {"cuda": True, "gpu": torch.cuda.get_device_name(0), "torch": torch.__version__}
        return {"cuda": False, "gpu": None, "torch": torch.__version__}
    except Exception as e:  # pragma: no cover
        return {"cuda": False, "gpu": None, "torch": f"unavailable: {e!r}"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("TOD_SERVER_NO_WARMUP") != "1":
        t = time.perf_counter()
        with _lock:
            ex.warmup()   # loads YOLO, GDINO, CLIP, both OCR recognisers; 2 passes for cuDNN caches
        _state["warm_ms"] = round((time.perf_counter() - t) * 1e3)
        _state["warm"] = True
        print(f"[extract_server] warm in {_state['warm_ms']} ms  {_gpu_info()}  OCR={ex.OCR_ENGINE}")
    yield


app = FastAPI(title="tod_papers extract", lifespan=lifespan)


def _decode(buf: bytes, scale: int) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "could not decode image (PNG/JPEG/WebP expected)")
    if scale > 1:
        # integer nearest-neighbour upscale == exact pixel replication (verified bit-identical)
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    return img


@app.get("/health")
def health() -> dict:
    return {"ok": True, **_state, **_gpu_info(), "ocr_engine": ex.OCR_ENGINE,
            "ocr_provider": ex.OCR_PROVIDER, "labels": ex._gdino is not None}


@app.post("/extract")
async def extract_endpoint(request: Request) -> dict:
    t_recv = time.perf_counter()
    if TOKEN and request.headers.get("authorization", "") != f"Bearer {TOKEN}":
        raise HTTPException(401, "bad or missing bearer token")
    ctype = request.headers.get("content-type", "")
    scale = int(request.query_params.get("scale", "1"))
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        f = form.get("image")
        if f is None:
            raise HTTPException(400, "multipart field 'image' missing")
        buf = await f.read()
        scale = int(form.get("scale", scale))
    elif ctype.startswith("application/json"):
        js = await request.json()
        if "image_b64" not in js:
            raise HTTPException(400, "json field 'image_b64' missing")
        buf = base64.b64decode(js["image_b64"])
        scale = int(js.get("scale", scale))
    else:
        buf = await request.body()
    t_body = time.perf_counter()
    # extract() is CPU/GPU bound and not re-entrant: run it off the event loop, one at a time
    import anyio

    return await anyio.to_thread.run_sync(_run, buf, scale, len(buf), (t_body - t_recv) * 1e3)


def _run(buf: bytes, scale: int, nbytes: int, body_ms: float) -> dict:
    t0 = time.perf_counter()
    frame = _decode(buf, scale)
    t_dec = time.perf_counter()
    with _lock:
        t_lock = time.perf_counter()
        boxes = ex.extract(frame)
        H, W = frame.shape[:2]
        out = [{**b.to_dict(), "describe": ex.describe(b, W, H)} for b in boxes]
        timings = dict(ex.LAST_TIMINGS)
        _state["calls"] += 1
    t_end = time.perf_counter()
    timings.update({
        "body_ms": round(body_ms, 1),
        "decode_ms": round((t_dec - t0) * 1e3, 1),
        "lock_wait_ms": round((t_lock - t_dec) * 1e3, 1),
        "server_ms": round((t_end - t0) * 1e3 + body_ms, 1),
    })
    return {"W": W, "H": H, "bytes": nbytes, "scale": scale, "boxes": out,
            "timings": {k: round(v, 1) for k, v in timings.items()}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("TOD_SERVER_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("TOD_SERVER_PORT", "8765")))
    a = ap.parse_args()
    import uvicorn

    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
