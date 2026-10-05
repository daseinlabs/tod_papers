"""/v1/systemone shim in front of a local tod-picker (deploy/gcp_fast.sh, VM `tod-fast`).

The loop's client speaks the public tod-api shape (`{"state": [...], "questions": {qid: {...}}}` ->
`{"id", "model", "answers", "usage", "latency_ms"}`); the picker container speaks its internal shape
(`POST /systemone {"state", "questions": [typed]}` + `X-Picker-Key`). This does what tod-api does between the
two (the same `parse_request` / `normalise_state`, fetched from tod-api at deploy time into tod_api_vendor/),
minus accounts and billing. No API key: it listens on 127.0.0.1 and is only reachable through the SSH tunnel.
"""
import json
import os
import time
import urllib.error
import urllib.request
import uuid

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse
from tod_api_vendor.jev import JevError, normalise_state, parse_request

PICKER = os.environ.get("PICKER_URL", "http://127.0.0.1:8080")
KEY = os.environ["TOD_PICKER_KEY"]
MAX_OPTIONS, MAX_QUESTIONS = int(os.environ.get("TOD_MAX_OPTIONS", "512")), int(os.environ.get("TOD_MAX_QUESTIONS", "16"))
app = FastAPI(title="tod-fast-shim")


def _err(status: int, code: str, msg: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": msg}}, status_code=status)


@app.get("/health")
@app.get("/healthz")
def healthz():
    try:
        with urllib.request.urlopen(PICKER + "/healthz", timeout=5) as r:
            return {"ok": True, "picker": json.load(r)}
    except Exception as e:  # picker still loading (503) or down
        return _err(503, "picker_unavailable", f"{type(e).__name__}: {e}")


@app.post("/v1/systemone")
def systemone(body: dict = Body(...)):
    st = time.perf_counter()
    try:
        questions, raw_state = parse_request(body, MAX_OPTIONS, MAX_QUESTIONS)
        if raw_state is None:
            raise JevError(422, "missing_state", "state is required")
        state, _ = normalise_state(raw_state, body.get("images"))
    except JevError as e:
        return _err(e.status, e.code, e.message)
    req = urllib.request.Request(PICKER + "/systemone", data=json.dumps({"state": state, "questions": questions}).encode(),
                                 headers={"Content-Type": "application/json", "X-Picker-Key": KEY})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        return _err(e.code if e.code in (413, 422, 503) else 502, "picker_error", e.read().decode(errors="replace")[:500])
    except Exception as e:
        return _err(502, "picker_unavailable", f"{type(e).__name__}: {e}")
    return {"id": "fast_" + uuid.uuid4().hex, "model": body.get("model") or out["model"], "answers": out["answers"],
            "usage": {"input_tokens": out["input_tokens"], "output_tokens": 0, "cost_usd": 0.0},
            "latency_ms": round((time.perf_counter() - st) * 1000, 3), "picker_ms": out.get("latency_ms")}
