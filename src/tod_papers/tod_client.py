"""Thin client for the TOD /v1/systemone API.

Verified against the live API 2026-10-01:
- Cloudflare rejects the default python-urllib User-Agent with 403 "error code: 1010".
  Always send a custom UA.
- Retries: 3 retries with 1/2/4 s backoff on 429/5xx, connection errors, SSL resets and read timeouts.
- HTTP 402 or an insufficient_credit body raises TodCreditExhausted at once (no retry); loop.py stops on it.
- Several questions per request; image goes in the state list as a data URL.
- Response per question: choice/noul/score + probabilities + confidence.
"""
from __future__ import annotations

import base64
import http.client
import io
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import numpy as np

TOD_URL = "https://tod.parseclab.ai/v1/systemone"
USER_AGENT = "tod_papers/0.1"


def _load_key() -> str:
    k = os.environ.get("TOD_API_KEY")
    if k:
        return k
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.getcwd(), os.path.join(here, "..", "..")):
        p = os.path.join(d, ".env")
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if line.startswith("TOD_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("TOD_API_KEY not set and no .env found")


def frame_to_data_url(frame_bgr: np.ndarray, fmt: str = "PNG") -> str:
    from PIL import Image

    rgb = frame_bgr[:, :, ::-1] if frame_bgr.ndim == 3 and frame_bgr.shape[2] == 3 else frame_bgr
    im = Image.fromarray(np.ascontiguousarray(rgb))
    b = io.BytesIO()
    im.save(b, fmt)
    mime = "image/png" if fmt.upper() == "PNG" else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(b.getvalue()).decode()


def choice(instructions: str, criteria: dict[str, str]) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def noul(instructions: str, yes: str, no: str) -> dict:
    return {"type": "noul", "instructions": instructions, "criteria": {"true": yes, "false": no}}


def score(instructions: str, levels: list[str]) -> dict:
    return {"type": "score", "instructions": instructions, "criteria": levels}


@dataclass
class Answer:
    qtype: str
    value: Any  # label for choice, P(yes) for noul, level index for score
    probabilities: dict[str, float]
    confidence: float
    entropy_confidence: float
    legend: dict[str, str] = field(default_factory=dict)

    @property
    def top(self) -> str:
        return max(self.probabilities, key=self.probabilities.get)


@dataclass
class TodResult:
    answers: dict[str, Answer]
    latency_ms: float
    input_tokens: int
    cost_usd: float
    request_id: str
    raw: dict

    def __getitem__(self, k: str) -> Answer:
        return self.answers[k]


RETRY_HTTP = (429, 500, 502, 503, 504)
# URLError covers refused/reset connections and DNS; ssl.SSLError/ConnectionError cover SSL EOF / resets raised
# while reading the body; TimeoutError/socket.timeout cover read timeouts; http.client errors cover a dropped
# response (RemoteDisconnected, IncompleteRead).
RETRY_EXC = (urllib.error.URLError, ConnectionError, TimeoutError, socket.timeout, ssl.SSLError,
             http.client.HTTPException, OSError)
BACKOFF_S = (1.0, 2.0, 4.0)


class TodCreditExhausted(RuntimeError):
    """HTTP 402 / insufficient_credit: no retry will help; the caller must stop (run 20261002_123058 skipped 27 ticks)."""


class TodClient:
    def __init__(self, api_key: str | None = None, timeout: float = 60.0, retries: int = 3):
        self.key = api_key or _load_key()
        self.timeout = timeout
        self.retries = retries
        self.total_cost = 0.0
        self.total_calls = 0
        self.total_retries = 0

    def _backoff(self, attempt: int, why: str, retry_after: str | None = None) -> None:
        d = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
        try:
            d = max(d, float(retry_after)) if retry_after else d
        except ValueError:
            pass
        self.total_retries += 1
        print(f"[tod] retry {attempt + 1}/{self.retries} in {d:.0f}s ({why[:160]})")
        time.sleep(d)

    def ask(
        self,
        questions: dict[str, dict],
        text: str | None = None,
        image_bgr: np.ndarray | None = None,
        image_data_url: str | None = None,
    ) -> TodResult:
        state: list[dict] = []
        if text:
            state.append({"text": text})
        if image_bgr is not None:
            image_data_url = frame_to_data_url(image_bgr)
        if image_data_url:
            state.append({"image": image_data_url})
        if not state:
            raise ValueError("need text and/or image")
        body = json.dumps({"state": state, "questions": questions}).encode()
        req = urllib.request.Request(
            TOD_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.load(r)
                if isinstance(data, dict) and "insufficient_credit" in json.dumps(data.get("error", "")):
                    raise TodCreditExhausted(f"TOD insufficient_credit: {str(data.get('error'))[:500]}")
                break
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="replace")[:500]
                if e.code == 402 or "insufficient_credit" in msg:
                    raise TodCreditExhausted(f"TOD HTTP {e.code}: {msg}") from e
                if e.code in RETRY_HTTP and attempt < self.retries:
                    last = e
                    self._backoff(attempt, f"HTTP {e.code}", e.headers.get("Retry-After"))
                    continue
                raise RuntimeError(f"TOD HTTP {e.code}: {msg}") from e
            except RETRY_EXC as e:
                # connection refused/reset, SSL EOF/reset, DNS hiccup, read timeout (runs 092612, 094930 crashed here)
                last = e
                if attempt < self.retries:
                    self._backoff(attempt, f"{type(e).__name__}: {e}")
                    continue
        else:
            raise RuntimeError(f"TOD unreachable after {self.retries} retries: {type(last).__name__}: {last}")
        wall = (time.perf_counter() - t0) * 1000
        answers = {}
        for qid, a in data["answers"].items():
            t = a["type"]
            if t == "choice":
                val = a["choice"]
            elif t == "noul":
                val = a["noul"]
            else:
                val = a["score"]
            answers[qid] = Answer(
                qtype=t,
                value=val,
                probabilities=a.get("probabilities", {}),
                confidence=a.get("confidence", 0.0),
                entropy_confidence=a.get("entropy_confidence", 0.0),
                legend=a.get("legend", {}),
            )
        usage = data.get("usage", {})
        self.total_cost += usage.get("cost_usd", 0.0)
        self.total_calls += 1
        return TodResult(
            answers=answers,
            latency_ms=wall,
            input_tokens=usage.get("input_tokens", 0),
            cost_usd=usage.get("cost_usd", 0.0),
            request_id=data.get("id", ""),
            raw=data,
        )


if __name__ == "__main__":
    c = TodClient()
    r = c.ask({"ok": noul("Is the passport valid?", "Valid", "Invalid")}, text="Passport expired in 1980.")
    print(r["ok"].value, r["ok"].probabilities, f"{r.latency_ms:.0f}ms")
