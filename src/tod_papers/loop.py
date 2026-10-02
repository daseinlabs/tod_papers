"""loop.py -- the TOD Set-of-Mark agent loop.

Each tick:  wait-until-stable grab
            -> REQUEST 1 (state, unmarked frame; concurrent with extract): screen, day, the
               booth facts the manual is keyed on (person at window, document on counter,
               open passport, tray open, passport under stamp, stamp mark, covered) and the
               inspection decisions (issuing country, expiry, photo) -- all read by TOD
               from the picture, nothing inferred from detector labels
            -> extract -> drop-target regions (stamp landing strip, counter shelf, desk;
               derived from detected boxes, else anchors.json fallback) -> som
            -> REQUEST 2 (action, SoM frame): the whole manual (manual.py) + what request 1
               says is true + the last 30 actions; questions action / source / target
            -> click/drag convention enforced on the pick -> execute -> verify -> log.

TOD makes every decision: what is on screen, what kind of input (click/drag/wait),
which numbered element, which drop target. One image per request. The only
geometry the loop adds is the drop-target regions TOD needs that no detector
finds; their source (derived/fallback) is written to every tick json.

Run (from repo root):
    .venv-loop\\Scripts\\python.exe -m tod_papers.loop --max-ticks 10
    .venv-loop\\Scripts\\python.exe -m tod_papers.loop --dry-run --max-ticks 3
"""
from __future__ import annotations

# io_win must be imported first: it sets per-monitor DPI awareness.
from . import io_win  # noqa: I001

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

import cv2
import numpy as np
import win32gui

from . import extract as ex
from .extract import Box
from . import manual as man
from .som import annotate
from .tod_client import TodClient, choice, noul

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

WINDOW_CLASS = "UnityWndClass"
WINDOW_TITLE = "PapersPlease"

SCREENS = {
    "menu": "a title/main menu on a plain background with selectable options (no booth visible)",
    "day_select": "a save / day-select / checkpoint list (may offer delete/trash icons)",
    "cutscene_or_text": "a full-screen story cutscene, newspaper or intro text on its own (no booth, desk or counter visible)",
    "bulletin": "a full-screen ministry bulletin shown on its own (no booth, desk or counter visible)",
    "booth_idle": "the inspection booth with the booth window empty (no person standing at it) and no passport on "
                  "the counter; a bulletin or rulebook may lie on the desk",
    "documents_on_desk": "the inspection booth with a person (entrant) standing at the booth window, or a "
                         "passport/documents lying on the counter or desk",
    "stamp_tray_open": "the inspection booth with the stamp bar slid out over the desk: large red DENIED and "
                       "green APPROVED stamps visible",
    "inspect_mode": "the booth in inspect/discrepancy mode (highlighted compare tool)",
    "day_end": "an end-of-day summary / family finances / next-day screen",
    "other": "anything else (loading, black screen, settings, dialogs)",
}

# day rules + the playing guide live in manual.py
DAY_RULES = man.DAY_RULES

RISKY_RE = re.compile(r"delete|trash|erase", re.I)


# --------------------------------------------------------------------------
# window / safety
# --------------------------------------------------------------------------


class AbortSafety(RuntimeError):
    pass


def find_game_window() -> int:
    hits = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetClassName(h) == WINDOW_CLASS:
            hits.append((h, win32gui.GetWindowText(h)))
        return True

    win32gui.EnumWindows(cb, None)
    titled = [h for h, t in hits if t == WINDOW_TITLE]
    if titled:
        return titled[0]
    if hits:
        return hits[0][0]
    raise LookupError("Papers, Please window (UnityWndClass) not found")


def try_foreground(hwnd: int) -> bool:
    io_win.focus(hwnd)
    for _ in range(10):
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.05)
        io_win.focus(hwnd)
    return win32gui.GetForegroundWindow() == hwnd


def ensure_foreground(hwnd: int) -> None:
    if not try_foreground(hwnd):
        fg = win32gui.GetForegroundWindow()
        raise AbortSafety(f"foreground is {fg} {win32gui.GetWindowText(fg)!r}, not the game ({hwnd}); aborting")


def is_foreground(hwnd: int) -> bool:
    return win32gui.GetForegroundWindow() == hwnd


def check_inside(hwnd: int, x: int, y: int) -> None:
    _, _, w, h = io_win.client_rect_physical(hwnd)
    if not (0 <= x < w and 0 <= y < h):
        raise AbortSafety(f"target ({x},{y}) outside game client {w}x{h}; aborting")


# --------------------------------------------------------------------------
# frame comparison
# --------------------------------------------------------------------------

DIFF_SCALE = 4      # compare at 1/4 (= Papers, Please native art scale at 2280 wide)
PIX_THRESH = 24     # grey levels for a pixel to count as changed


def _small(a: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(cv2.resize(a, (a.shape[1] // DIFF_SCALE, a.shape[0] // DIFF_SCALE),
                                   interpolation=cv2.INTER_NEAREST), cv2.COLOR_BGR2GRAY)


def frame_hash(a: np.ndarray) -> str:
    return hashlib.md5(_small(a).tobytes()).hexdigest()[:12]


def change_map(a: np.ndarray, b: np.ndarray) -> np.ndarray | None:
    if a is None or b is None or a.shape != b.shape:
        return None
    return cv2.absdiff(_small(a), _small(b)) > PIX_THRESH


def changed_frac(m: np.ndarray | None, box: Box | None = None, pad: float = 0.15) -> float:
    """Fraction of changed pixels, globally or within `box` (full-res coords, padded)."""
    if m is None:
        return 1.0
    if box is None:
        return float(m.mean())
    s = DIFF_SCALE
    px, py = int(box.w * pad), int(box.h * pad)
    x1, y1 = max(0, (box.x1 - px) // s), max(0, (box.y1 - py) // s)
    x2, y2 = min(m.shape[1], (box.x2 + px) // s + 1), min(m.shape[0], (box.y2 + py) // s + 1)
    roi = m[y1:y2, x1:x2]
    return float(roi.mean()) if roi.size else 0.0


def wait_stable(grab, interval=0.15, thresh=0.01, max_wait=3.0):
    """Grab pairs `interval` apart until the changed-pixel fraction drops below
    `thresh` (ambient motion such as walking crowds measured 0.0003-0.0045) or
    `max_wait` elapses. Returns (frame, waited_s, last_frac, stable, ambient_mask):
    ambient_mask marks pixels that were moving on their own just before the
    action (dilated), so the post-action check can ignore them."""
    t0 = time.perf_counter()
    prev = grab()
    while True:
        time.sleep(interval)
        cur = grab()
        m = change_map(prev, cur)
        f = changed_frac(m)
        waited = time.perf_counter() - t0
        if f < thresh or waited >= max_wait:
            amb = cv2.dilate(m.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0 if m is not None else None
            return cur, waited, f, f < thresh, amb
        prev = cur


def effect_map(before: np.ndarray, after: np.ndarray, ambient: np.ndarray | None) -> np.ndarray | None:
    m = change_map(before, after)
    if m is not None and ambient is not None and ambient.shape == m.shape:
        m = m & ~ambient
    return m


# --------------------------------------------------------------------------
# describing boxes
# --------------------------------------------------------------------------


BG_DESC = "no element - the screen itself (click it to advance a cutscene/text screen that has no button)"
def describe(b: Box, W: int, H: int) -> str:
    """Criteria text for one box. extract.describe() is the single source (kind,
    OCR text or caption, coarse position); the loop only adds its synthetic
    'background' option."""
    if b.kind == "background":
        return BG_DESC
    if b.kind == "region":
        return f"drop target - {b.caption}"
    return ex.describe(b, W, H)


def short(desc: str, n: int = 70) -> str:
    return desc if len(desc) <= n else desc[: n - 3] + "..."


# --------------------------------------------------------------------------
# stuck detection
# --------------------------------------------------------------------------


def _iou(a: Box, b: Box) -> float:
    ix = max(0, min(a.x2, b.x2) - max(a.x1, b.x1))
    iy = max(0, min(a.y2, b.y2) - max(a.y1, b.y1))
    inter = ix * iy
    u = a.area + b.area - inter
    return inter / u if u else 0.0


def _same_element(a: Box, b: Box) -> bool:
    """Match an element across ticks (ids are re-assigned every tick)."""
    if a.kind == "background" or b.kind == "background":
        return a.kind == b.kind
    i = _iou(a, b)
    return i > 0.5 or (bool(a.text) and a.text == b.text and i > 0.1)


@dataclass
class _Fail:
    action: str
    box: Box
    desc: str
    count: int = 0
    banned_until: int = -1   # tick index (exclusive) until which the box is excluded
    last_tick: int = 0


@dataclass
class StuckTracker:
    """Per-run memory of (action, element) pairs that produced no visible change.

    After `limit` no-effect tries the element is excluded from the `source`
    options for `ban_ticks` ticks. Decay: when a ban expires the count drops to
    limit-1 (one more failed try re-bans it); entries untouched for
    `forget_ticks` are dropped; any effective action on the element clears it."""
    limit: int = 2
    ban_ticks: int = 6
    forget_ticks: int = 15
    fails: list = field(default_factory=list)

    def _find(self, action: str, b: Box):
        for f in self.fails:
            if f.action == action and _same_element(f.box, b):
                return f
        return None

    def record(self, tick: int, action: str, b: Box, desc: str, changed: bool):
        f = self._find(action, b)
        if changed:
            if f is not None:
                self.fails.remove(f)
            return None
        if f is None:
            f = _Fail(action, b, desc)
            self.fails.append(f)
        f.box, f.desc, f.last_tick = b, desc, tick
        f.count += 1
        if f.count >= self.limit:
            f.banned_until = tick + 1 + self.ban_ticks
        return f

    def decay(self, tick: int) -> None:
        keep = []
        for f in self.fails:
            if f.banned_until >= 0 and tick >= f.banned_until:
                f.banned_until = -1
                f.count = self.limit - 1
            if f.banned_until < 0 and tick - f.last_tick > self.forget_ticks:
                continue
            keep.append(f)
        self.fails = keep

    def banned(self, tick: int, b: Box):
        # any action kind: TOD's `source` answer is shared between click and drag
        for f in self.fails:
            if f.banned_until > tick and _same_element(f.box, b):
                return f
        return None

    def active_bans(self, tick: int) -> list:
        return [f for f in self.fails if f.banned_until > tick]


# --------------------------------------------------------------------------
# prompt building
# --------------------------------------------------------------------------


BOOTH_SCREENS = {"booth_idle", "documents_on_desk", "stamp_tray_open", "inspect_mode"}

DAY_Q = choice(
    "Which in-game day is it (from bulletin, date or clock text if visible; otherwise unknown)?",
    {"1": "Day 1 / Nov 23", "2": "Day 2 / Nov 24", "3": "Day 3 / Nov 25", "later": "Day 4 or later",
     "unknown": "not determinable yet"},
)


def _small_for_send(frame: np.ndarray, args) -> np.ndarray:
    H, W = frame.shape[:2]
    if args.send_width and W > args.send_width:
        return cv2.resize(frame, (args.send_width, int(H * args.send_width / W)), interpolation=cv2.INTER_AREA)
    return frame


def state_probe(tod: TodClient, frame: np.ndarray, args, day: str = "unknown"):
    """REQUEST 1: plain (unmarked) frame + one-line text + screen, day and the
    manual's state/inspection questions (manual.state_questions). Asked inside
    the long prompt over the marked frame `screen` was near-uniform; asked on its
    own it was right on 4/4 frames. Runs concurrently with extract()."""
    today = man.DAY_DATES.get(day, man.DAY_DATES["1"])
    q = {"screen": choice("Which kind of screen is currently shown?", dict(SCREENS)), "day": DAY_Q}
    q.update(man.state_questions(today))
    small = _small_for_send(frame, args)
    return tod.ask(q, text=man.STATE_TEXT, image_data_url=encode_image(small, args.send_format, args.jpeg_quality))


def parse_state(res) -> dict:
    """{question: {value, p, probs}}; p = P(true) for noul, P(chosen) for choice."""
    out = {}
    for k, a in res.answers.items():
        if a.qtype == "noul":
            p = p_true(a)
            out[k] = {"value": p >= 0.5, "p": round(p, 3), "probs": a.probabilities}
        else:
            out[k] = {"value": a.value, "p": round(float(a.probabilities.get(str(a.value), 0.0)), 3),
                      "probs": a.probabilities}
    return out


_STATE_ABBR = {"person_at_window": "person", "document_on_counter_shelf": "counter",
               "document_open_on_desk": "open", "stamp_tray_open": "tray", "document_under_stamp_heads": "under",
               "passport_shows_stamp_mark": "mark", "bulletin_or_rulebook_covering_desk": "cover",
               "expiry_after_today": "exp_ok", "photo_matches_person": "photo"}


def state_line(state: dict) -> str:
    """Compact all-answers line (p = P(yes)) for the console and the overlay banner."""
    bits = []
    if "screen" in state:
        bits.append(f"{state['screen']['value']}:{state['screen']['p']:.2f}")
    for k, ab in _STATE_ABBR.items():
        if k in state:
            bits.append(f"{ab}={state[k]['p']:.2f}")
    if "issuing_country" in state:
        bits.append(f"iss={state['issuing_country']['value']}:{state['issuing_country']['p']:.2f}")
    if "day" in state:
        bits.append(f"day={state['day']['value']}")
    return " ".join(bits)


# --------------------------------------------------------------------------
# drop-target regions (stamp landing strip, counter shelf, desk)
# --------------------------------------------------------------------------

ANCHORS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anchors.json")
_ANCHORS = None

REGION_CAPS = {
    "stamp_landing_approved": "stamp landing strip (under the APPROVED stamp head)",
    "stamp_landing_denied": "stamp landing strip (under the DENIED stamp head)",
    "counter_shelf": "counter shelf (hand documents back here)",
    "desk": "desk (drop documents here to read them)",
}


def _anchor(name: str, W: int, H: int) -> Box:
    global _ANCHORS
    if _ANCHORS is None:
        with open(ANCHORS_FILE, encoding="utf-8") as fh:
            _ANCHORS = json.load(fh)
    nw, nh = _ANCHORS.get("native_size", [570, 320])
    x1, y1, x2, y2 = _ANCHORS[name]
    sx, sy = W / nw, H / nh
    return Box(int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy), "", "region", 0.0, caption=REGION_CAPS[name])


_STAMP_TXT = re.compile(r"^\W*(APPRO\w*|DENI\w*)\W*$", re.I)


def _stamp_side(b: Box, frame: np.ndarray):
    t = (b.text or "").upper()
    if _STAMP_TXT.match(t):
        return "approved" if t.strip(" '\"").startswith("APPRO") else "denied"
    if b.caption in ("green rubber stamp", "red rubber stamp"):
        return "approved" if b.caption.startswith("green") else "denied"
    if b.caption == "rubber stamp":
        # Grounding DINO 'rubber stamp' hits the knob: decide by the colour just below it
        y1, y2 = b.y2, min(frame.shape[0], b.y2 + max(8, b.h // 2))
        roi = frame[y1:y2, b.x1:b.x2]
        if roi.size == 0:
            return None
        bgr = roi.reshape(-1, 3).mean(0)
        if bgr[1] > bgr[2] + 15:
            return "approved"
        if bgr[2] > bgr[1] + 15:
            return "denied"
    return None


def derive_regions(boxes: list, frame: np.ndarray, state: dict):
    """Drop targets no detector finds, derived from this tick's detected boxes:
    - stamp landing strip, one per stamp: directly beneath the stamp body (x span
      of the detected APPROVED/DENIED stamp boxes, y from the body's bottom edge
      down ~12% of the frame). Offered when TOD says the tray is open or a stamp
      was detected.
    - counter shelf: under the booth window -- top = bottom of the person box at
      the window (or a document lying on the counter), right edge = the shutter
      lever / window frame, bottom = the drawer row.
    - desk: free desk to the right of the counter shelf, below the tray.
    Anything that cannot be derived (or fails a sanity check) comes from
    anchors.json and is marked target_source=fallback.
    Returns (regions, {name: 'derived'|'fallback'})."""
    H, W = frame.shape[:2]
    regions: list = []
    src: dict = {}

    # --- stamp landing strips
    sides: dict = {}
    for b in boxes:
        cx, cy = b.center
        if cx < 0.4 * W or not (0.2 * H < cy < 0.7 * H):
            continue
        side = _stamp_side(b, frame)
        if side:
            sides.setdefault(side, []).append(b)
    if man.yes(state, "stamp_tray_open") or sides:
        for side in ("denied", "approved"):
            name = f"stamp_landing_{side}"
            bs = sides.get(side)
            r = None
            if bs:
                x1, x2 = min(b.x1 for b in bs), max(b.x2 for b in bs)
                bottom = max(b.y2 for b in bs)
                cx = (x1 + x2) // 2
                hw = max(int(0.35 * (x2 - x1)), int(0.02 * W))
                r = Box(cx - hw, bottom + int(0.01 * H), cx + hw, bottom + int(0.13 * H), "", "region", 0.0,
                        caption=REGION_CAPS[name])
                if not (0.4 * W < cx < W and 0.35 * H < r.y1 < 0.8 * H and r.y2 <= H):
                    r = None
            if r is not None:
                src[name] = "derived"
            else:
                r = _anchor(name, W, H)
                src[name] = "fallback"
            regions.append(r)

    # --- counter shelf
    def left(b, maxfrac=0.36) -> bool:
        return b.center[0] < maxfrac * W

    persons = [b for b in boxes if b.caption == "person face" and left(b) and 0.5 * H < b.y2 < 0.78 * H]
    levers = [b for b in boxes if b.caption == "lever handle" and left(b, 0.4)]
    counter_docs = [b for b in boxes if b.caption == ex.COUNTER_CAP and left(b)]
    shelf = None
    if persons or counter_docs:
        if persons:
            p = max(persons, key=lambda b: b.area)
            y1 = p.y2
            x1 = max(0, p.x1 - int(0.05 * W))
            x2 = max(levers, key=lambda b: b.area).x2 if levers else p.x2 + int(0.05 * W)
        else:
            d = counter_docs[0]
            y1 = d.y1 - int(0.02 * H)
            x1 = max(0, d.x1 - int(0.08 * W))
            x2 = max(levers, key=lambda b: b.area).x2 if levers else d.x2 + int(0.08 * W)
        drawers = [b.y1 for b in boxes if left(b) and b.y1 > y1 + 0.08 * H]
        y2 = min([y1 + int(0.17 * H)] + [y - int(0.005 * H) for y in drawers])
        cand = Box(x1, y1, x2, y2, "", "region", 0.0, caption=REGION_CAPS["counter_shelf"])
        if 0.55 * H <= y1 <= 0.75 * H and cand.h >= 0.06 * H and x2 <= 0.4 * W and cand.w >= 0.15 * W:
            shelf = cand
    if shelf is not None:
        src["counter_shelf"] = "derived"
    else:
        shelf = _anchor("counter_shelf", W, H)
        src["counter_shelf"] = "fallback"
    regions.append(shelf)

    # --- desk: right of the shelf, below the tray
    if src["counter_shelf"] == "derived":
        x1 = shelf.x2 + int(0.03 * W)
        desk = Box(x1, int(0.72 * H), min(W - 1, x1 + int(0.25 * W)), int(0.95 * H), "", "region", 0.0,
                   caption=REGION_CAPS["desk"])
        src["desk"] = "derived"
    else:
        desk = _anchor("desk", W, H)
        src["desk"] = "fallback"
    regions.append(desk)
    return regions, src


# --------------------------------------------------------------------------
# request 2 questions + the click/drag convention
# --------------------------------------------------------------------------

ACTION_RULE = (
    "Constraint: the loudspeaker/horn, the APPROVED and DENIED stamps, buttons/menu text and page corners are "
    "CLICK only; documents (passport, papers, bulletin, rulebook), the stamp tray tab and the shutter lever are "
    "DRAG only. Drop targets (stamp landing strip, counter shelf, desk) are only the end point of a drag."
)


def build_questions(src_ids: dict, tgt_ids: dict) -> dict:
    return {
        "action": choice(
            "Following the manual and what is currently true on screen, what kind of mouse input is the next "
            "step? " + ACTION_RULE,
            {
                "click": "click one numbered element (loudspeaker, a stamp, a button/menu text, a page corner)",
                "drag": "drag one numbered element (a document, the stamp tray tab, the lever) onto a drop target",
                "wait": "do nothing this turn (someone is walking in / a screen is changing)",
            },
        ),
        "source": choice(
            "Following the manual and what is currently true on screen: which numbered element is clicked "
            "next, or, for a drag, picked up? Use the number drawn on its marker.",
            src_ids,
        ),
        "target": choice(
            "If the next input is a drag: onto which numbered drop target or element should the dragged item be "
            "released? (stamp landing strip = under a stamp so it can be stamped; counter shelf = hand documents "
            "back; desk = read a document / put papers aside / pull the tray tab left.) Ignored for a click.",
            tgt_ids,
        ),
    }


def _cls(b, booth: bool):
    if b is None:
        return None
    if b.kind == "background":
        return "click"
    return man.input_class(b, booth)


def enforce_input(action: str, src: str, res, idmap: dict, src_ids: dict, booth: bool):
    """Apply the manual's click-only / drag-only convention to TOD's pick.
    On a conflict (e.g. drag on a stamp) take the better of
      (a) same element, its allowed input:  P(src) * P(allowed action)
      (b) same input, best element allowed for it:  P(action) * P(element)
    Returns (action, src, note); note == '' when nothing changed."""
    if action not in ("click", "drag") or not src.isdigit():
        return action, src, ""
    cls = _cls(idmap.get(int(src)), booth)
    if cls in (None, action):
        return action, src, ""
    pa, ps = res["action"].probabilities, res["source"].probabilities
    opts = []
    if cls in ("click", "drag"):
        opts.append((float(ps.get(src, 0)) * float(pa.get(cls, 0)), cls, src))
    best = None
    for k in src_ids:
        if _cls(idmap.get(int(k)), booth) in (None, action) and (best is None or ps.get(k, 0) > ps.get(best, 0)):
            best = k
    if best:
        opts.append((float(pa.get(action, 0)) * float(ps.get(best, 0)), action, best))
    if not opts:
        return "wait", src, f"{action} on #{src} ({cls}-only), no compatible option -> wait"
    _, a2, s2 = max(opts, key=lambda o: o[0])
    note = (f"TOD chose {action} #{src} but that element is {cls}-only -> "
            + (f"coerced to {a2} #{s2}" if s2 == src else f"kept {a2}, re-picked #{s2} (best {a2}-able element)"))
    return a2, s2, note


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------


def top(p: dict[str, float], k: int = 3):
    return [(lab, round(v, 3)) for lab, v in sorted(p.items(), key=lambda kv: -kv[1])[:k]]


def p_true(ans) -> float:
    p = ans.probabilities or {}
    for k in ("true", "yes", "True"):
        if k in p:
            return float(p[k])
    return float(ans.value) if isinstance(ans.value, (int, float)) else (1.0 if ans.value is True else 0.0)


def write_summary(run_dir: str, rows: list[dict], meta: dict) -> None:
    lines = [f"# Run {os.path.basename(run_dir)}", ""]
    for k, v in meta.items():
        lines.append(f"- {k}: {v}")
    lines += ["", "| tick | screen | state (TOD, request 1) | manual step | action | source desc | p(src) | effect |",
              "|---:|---|---|---|---|---|---:|---|"]
    for r in rows:
        d = str(r.get("src_desc", "-")).replace("|", "/")
        if r.get("tgt_desc"):
            d += " -> " + str(r["tgt_desc"]).replace("|", "/")
        lines.append(f"| {r['tick']} | {r.get('screen', '-')} | {r.get('state', '-')} | {r.get('step', '-')} | "
                     f"{r.get('action', '-')} | {short(d, 90)} | {r.get('p_src', 0):.2f} | {r.get('effect', '-')} |")
    with open(os.path.join(run_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def encode_image(img: np.ndarray, fmt: str = "png", quality: int = 90) -> str:
    """Data URL for TOD. cv2 encoders are much faster than PIL PNG at this size."""
    import base64
    if fmt == "png":
        ok, buf = cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, 3])
        mime = "image/png"
    else:
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        mime = "image/jpeg"
    if not ok:
        raise RuntimeError("image encode failed")
    return f"data:{mime};base64," + base64.b64encode(buf.tobytes()).decode()


def prepare(frame: np.ndarray, boxes: list[Box], state: dict, history, day: str, args,
            stuck: "StuckTracker | None" = None, tick: int = 0) -> dict:
    """Everything between (extract + request 1) and request 2: drop-target
    regions, Set-of-Mark annotation, criteria (extract.describe), questions,
    the manual text and the encoded (downscaled) image."""
    t0 = time.perf_counter()
    H, W = frame.shape[:2]
    screen = state.get("screen", {}).get("value", "")
    booth = screen in BOOTH_SCREENS
    regions, region_src = derive_regions(boxes, frame, state) if booth else ([], {})
    banned = (lambda b: stuck.banned(tick, b) is not None) if stuck is not None else None
    annotated, idmap = annotate(frame, list(boxes) + regions, max_marks=args.max_marks - 1 + len(regions),
                                excluded=banned)
    if not booth:  # text/cutscene screens without a button are advanced by clicking the screen itself
        idmap[len(idmap) + 1] = Box(W // 4, H // 4, 3 * W // 4, 3 * H // 4, "", "background", 0.0)
    desc = {str(i): describe(b, W, H) for i, b in idmap.items()}
    banned_ids, ban_lines = {}, []
    if stuck is not None:
        for i, b in idmap.items():
            f = stuck.banned(tick, b)
            if f is not None:
                banned_ids[str(i)] = f
        for f in stuck.active_bans(tick):
            verb = "Clicking" if f.action == "click" else "Dragging"
            ban_lines.append(f"{verb} '{short(f.desc, 60)}' did nothing (tried {f.count}x); excluded for "
                             f"{f.banned_until - tick} more tick(s)")
    # regions are drop targets only; the background is a click source only
    src_ids = {k: v for k, v in desc.items() if k not in banned_ids and idmap[int(k)].kind != "region"}
    tgt_ids = {k: v for k, v in desc.items() if idmap[int(k)].kind != "background"} or dict(desc)
    region_ids = {k: idmap[int(k)].caption for k in desc if idmap[int(k)].kind == "region"}
    region_info = {}
    for k, cap in region_ids.items():
        name = next(n for n, c in REGION_CAPS.items() if c == cap)
        b = idmap[int(k)]
        region_info[name] = {"id": k, "box": [b.x1, b.y1, b.x2, b.y2], "target_source": region_src.get(name, "?")}
    questions = build_questions(src_ids, tgt_ids)
    state_text = man.build(state, history, day, ban_lines)
    send = _small_for_send(annotated, args)
    url = encode_image(send, args.send_format, args.jpeg_quality)
    return dict(annotated=annotated, idmap=idmap, desc=desc, banned_ids=banned_ids, src_ids=src_ids,
                tgt_ids=tgt_ids, regions=region_info, booth=booth, questions=questions, state_text=state_text,
                image_url=url, image_kb=round(len(url) * 3 / 4 / 1024, 1),
                prep_ms=round((time.perf_counter() - t0) * 1e3, 1))


_PROBE_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="state-probe")


def _timed(fn, *a):
    t = time.perf_counter()
    r = fn(*a)
    return r, (time.perf_counter() - t) * 1e3


def _clean_state(state: dict) -> dict:
    return {k: {"value": v["value"], "p": v["p"], "probs": {a: round(b, 4) for a, b in v["probs"].items()}}
            for k, v in state.items()}


def decide(res, P: dict) -> dict:
    """TOD's request-2 answers -> the input to perform (after the click/drag
    convention). Pure: no I/O."""
    action = res["action"].value
    src = str(res["source"].value)
    tod_pick = (action, src)
    action, src, note = enforce_input(action, src, res, P["idmap"], P["src_ids"], P["booth"])
    tgt = str(res["target"].value)
    if action == "drag" and tgt == src:
        # dropping an item on itself is a no-op: take TOD's best other target
        alt = [k for k, _ in sorted(res["target"].probabilities.items(), key=lambda kv: -kv[1]) if k != src]
        if alt:
            tgt = alt[0]
    return dict(action=action, src=src, tgt=tgt, note=note, tod_pick=tod_pick,
                p_src=float(res["source"].probabilities.get(src, 0.0)))


def offline(args) -> int:
    """--frames: request 1 + extract -> prepare -> request 2 per saved frame, each
    with fresh state (no history). Never looks for the game window, never sends input."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out or os.path.join(ROOT, "runs", ts + "_offline")
    os.makedirs(out_dir, exist_ok=True)
    tod = TodClient(timeout=90)
    ex.warmup()
    results = []
    for path in args.frames:
        frame = cv2.imread(path)
        if frame is None:
            print(f"[offline] cannot read {path}")
            continue
        t0 = time.perf_counter()
        fut = _PROBE_POOL.submit(_timed, state_probe, tod, frame, args, "unknown")
        boxes = ex.extract(frame)
        t_ex = (time.perf_counter() - t0) * 1e3
        probe, t_probe = fut.result()
        state = parse_state(probe)
        P = prepare(frame, boxes, state, deque(), "unknown", args)
        t1 = time.perf_counter()
        res = tod.ask(P["questions"], text=P["state_text"], image_data_url=P["image_url"])
        t_tod = (time.perf_counter() - t1) * 1e3
        D = decide(res, P)
        step = man.situation(state, "1")
        r = {
            "frame": path, "n_boxes_raw": len(boxes), "n_sources": len(P["src_ids"]), "n_targets": len(P["tgt_ids"]),
            "extract_ms": round(t_ex), "state_ms": round(t_probe), "tod_ms": round(t_tod),
            "image_kb": P["image_kb"], "state": _clean_state(state), "state_line": state_line(state),
            "manual_step_for_state (diagnostic, not sent)": step, "regions": P["regions"],
            "target_source": {n: v["target_source"] for n, v in P["regions"].items()},
            "state_text": P["state_text"], "state_text_words": len(P["state_text"].split()),
            "criteria": P["desc"],
            "answers": {q: {"choice": a.value, "probabilities": a.probabilities} for q, a in res.answers.items()},
            "action_top3": top(res["action"].probabilities),
            "source_top3": [(k, P["desc"].get(k, k), p) for k, p in top(res["source"].probabilities)],
            "target_top3": [(k, P["desc"].get(k, k), p) for k, p in top(res["target"].probabilities)],
            "decision": {**D, "src_desc": P["desc"].get(D["src"], "-"),
                         "tgt_desc": P["desc"].get(D["tgt"], "-") if D["action"] == "drag" else None},
        }
        results.append(r)
        stem = os.path.join(out_dir, os.path.basename(os.path.dirname(os.path.abspath(path))) + "_"
                            + os.path.splitext(os.path.basename(path))[0])
        cv2.imwrite(stem + "_som.png", P["annotated"])
        try:
            from . import overlay
            res.answers["screen"] = probe["screen"]
            overlay.render(P["annotated"], P["idmap"], res, stem + "_viz.png", descriptions=P["desc"],
                           executed="(offline)", effect="-", state_line=r["state_line"],
                           chosen=(D["action"], D["src"], D["tgt"]), note=D["note"])
        except Exception as e:
            print(f"[offline] overlay failed: {e}")
        with open(stem + ".json", "w", encoding="utf-8") as fh:
            json.dump(r, fh, indent=1)
        print(f"[offline] {path}: extract={t_ex:.0f}ms state={t_probe:.0f}ms tod={t_tod:.0f}ms "
              f"words={r['state_text_words']}\n           state: {r['state_line']}\n           step: {step}"
              f"\n           action={r['action_top3']} regions={r['target_source']}")
        for k, d, p in r["source_top3"]:
            print(f"           source #{k} p={p:.3f} {d}")
        for k, d, p in r["target_top3"]:
            print(f"           target #{k} p={p:.3f} {d}")
        print(f"           decision: {D['action']} #{D['src']} -> #{D['tgt']} {D['note']}")
    with open(os.path.join(out_dir, "offline.json"), "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=1)
    print(f"[offline] TOD calls={tod.total_calls} cost=${tod.total_cost:.4f} -> {out_dir}")
    return 0


def run(args) -> int:
    hwnd = find_game_window()
    x, y, w, h = io_win.client_rect_physical(hwnd)
    print(f"[loop] game hwnd={hwnd} client=({x},{y}) {w}x{h} dry_run={args.dry_run}")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(ROOT, "runs", ts)
    os.makedirs(run_dir, exist_ok=True)
    print(f"[loop] logging to {run_dir}")
    park = (min(max(int(w * args.park[0]), 0), w - 3), min(max(int(h * args.park[1]), 0), h - 3))

    grab = io_win.Grabber(hwnd)
    tod = TodClient(timeout=90)
    print("[loop] warming up extractor ...")
    t = time.perf_counter()
    ex.warmup()
    print(f"[loop] warmup {1e3 * (time.perf_counter() - t):.0f} ms")

    history: deque = deque(maxlen=args.history)
    stuck = StuckTracker(limit=args.stuck_limit, ban_ticks=args.ban_ticks)
    rows: list[dict] = []
    day = "unknown"
    last_state: dict = {}
    fg_misses = 0
    overlay = None
    if args.viz:
        try:
            from . import overlay as overlay  # noqa: F811
        except Exception as e:
            print(f"[loop] overlay unavailable: {e}")

    def park_cursor():
        if not args.dry_run and is_foreground(hwnd):
            io_win.move(hwnd, *park)

    try:
        for tick in range(args.max_ticks):
            rec: dict = {"tick": tick, "time": time.time()}
            row: dict = {"tick": tick}
            rows.append(row)
            stuck.decay(tick)
            # capture needs the game unoccluded (Desktop Duplication grabs the screen region)
            if not try_foreground(hwnd):
                fg_misses += 1
                print(f"[tick {tick:03d}] game is not foreground ({fg_misses}x); not acting")
                row.update(action="none", effect="skipped: game not foreground")
                if fg_misses >= 3:
                    raise AbortSafety("game window lost foreground 3 ticks in a row")
                time.sleep(1.0)
                continue
            fg_misses = 0
            park_cursor()
            time.sleep(0.1)
            frame, waited, af, stable, ambient = wait_stable(grab.grab, interval=0.15, thresh=args.stable_thresh,
                                                             max_wait=args.max_anim_wait)
            rec.update(frame_shape=list(frame.shape), frame_hash=frame_hash(frame),
                       anim_wait_s=round(waited, 2), anim_frac=round(af, 5), stable=stable)
            if not stable:
                print(f"[tick {tick:03d}] screen still animating after {waited:.1f}s (frac {af:.4f}); proceeding anyway")

            # ---- REQUEST 1 (state, unmarked frame) concurrently with extract -------
            t0 = time.perf_counter()
            fut = _PROBE_POOL.submit(_timed, state_probe, tod, frame, args, day)
            boxes = ex.extract(frame)
            rec["extract_ms"] = round((time.perf_counter() - t0) * 1e3, 1)
            rec["extract_timings"] = {k: round(v, 1) for k, v in getattr(ex, "LAST_TIMINGS", {}).items()}
            try:
                probe, rec["state_ms"] = fut.result()
                state = parse_state(probe)
            except RuntimeError as e:  # never act on a stale picture of the screen
                print(f"[tick {tick:03d}] state request failed: {e}; skipping tick")
                rec.update(tod_error=f"state: {e}", executed="none (state request error)")
                row.update(action="none", effect="skipped: state request error")
                with open(os.path.join(run_dir, f"tick_{tick:04d}.json"), "w", encoding="utf-8") as fh:
                    json.dump(rec, fh, indent=1)
                time.sleep(2.0)
                continue
            rec["probe_wait_ms"] = round((time.perf_counter() - t0) * 1e3 - rec["extract_ms"], 1)
            screen = state["screen"]["value"]
            dv = state.get("day", {})
            if dv.get("value") in DAY_RULES and dv.get("p", 0) >= 0.5:
                day = dv["value"]
            step = man.situation(state, day if day in DAY_RULES else "1")
            sline = state_line(state)
            rec.update(state=_clean_state(state), state_line=sline,
                       manual_step_for_state=list(step))  # diagnostic only, never sent to TOD

            # ---- REQUEST 2 (action, SoM frame) -------------------------------------
            P = prepare(frame, boxes, state, history, day, args, stuck=stuck, tick=tick)
            annotated, idmap, desc, banned_ids = P["annotated"], P["idmap"], P["desc"], P["banned_ids"]
            rec["prep_ms"] = P["prep_ms"]
            rec["regions"] = P["regions"]
            rec["target_source"] = {n: v["target_source"] for n, v in P["regions"].items()}
            t1 = time.perf_counter()
            try:
                res = tod.ask(P["questions"], text=P["state_text"], image_data_url=P["image_url"])
            except RuntimeError as e:  # network/HTTP failure: log, skip the tick, never act blind
                print(f"[tick {tick:03d}] TOD request failed: {e}; skipping tick")
                rec.update(tod_error=str(e), executed="none (TOD error)")
                row.update(action="none", effect="skipped: TOD error")
                with open(os.path.join(run_dir, f"tick_{tick:04d}.json"), "w", encoding="utf-8") as fh:
                    json.dump(rec, fh, indent=1)
                time.sleep(2.0)
                continue
            rec["tod_ms"] = round((time.perf_counter() - t1) * 1e3, 1)
            rec["tod_request_id"] = res.request_id
            res.answers["screen"] = probe["screen"]  # overlay shows it alongside the other answers

            D = decide(res, P)
            action, src, tgt = D["action"], D["src"], D["tgt"]
            p_src = D["p_src"]
            if D["note"]:
                print(f"[tick {tick:03d}] input convention: {D['note']}")
            rec.update(
                state_text=P["state_text"],
                descriptions=desc,
                excluded={k: v.desc for k, v in banned_ids.items()},
                boxes={str(i): b.to_dict() for i, b in idmap.items()},
                answers={q: {"choice": a.value, "probabilities": a.probabilities, "confidence": a.confidence}
                         for q, a in res.answers.items()},
                tod_pick=list(D["tod_pick"]), input_convention=D["note"] or None,
            )
            sb = idmap.get(int(src)) if src.isdigit() else None
            tb = idmap.get(int(tgt)) if tgt.isdigit() else None
            src_desc = desc.get(src, "-")
            print(
                f"[tick {tick:03d}] boxes={len(idmap)} wait={waited:.2f}s extract={rec['extract_ms']:.0f}ms "
                f"state={rec.get('state_ms', 0):.0f}ms tod={rec['tod_ms']:.0f}ms\n"
                f"           state: {sline}\n           (manual step {step[0]}: {step[1]})\n"
                f"           action={top(res['action'].probabilities)} source={top(res['source'].probabilities)} "
                f"({short(src_desc)}) target={top(res['target'].probabilities, 2)}"
                + (f" excluded={sorted(banned_ids, key=int)}" if banned_ids else "")
            )
            row.update(screen=screen, state=man.state_summary(state), step=step[0], action=action,
                       src_desc=src_desc, p_src=p_src)
            if action == "drag":
                row["tgt_desc"] = desc.get(tgt, tgt)

            # ---- safety gates ----------------------------------------------------
            veto = None
            if action in ("click", "drag") and sb is not None and screen in ("day_select", "menu") \
                    and RISKY_RE.search(src_desc) and p_src <= 0.9:
                veto = f"declined '{short(src_desc, 50)}' on {screen} (destructive-looking, p={p_src:.2f} <= 0.9)"
            # ---- execute ---------------------------------------------------------
            if veto:
                executed = "vetoed: " + veto
            elif action == "click" and sb is not None:
                cx, cy = sb.center
                check_inside(hwnd, cx, cy)
                executed = f"click #{src} at ({cx},{cy})"
                if not args.dry_run:
                    if not try_foreground(hwnd):
                        executed = "none (game not foreground at action time)"
                    else:
                        io_win.click(hwnd, cx, cy, settle=args.settle)
            elif action == "drag" and sb is not None and tb is not None and tb is not sb:
                (ax, ay), (bx, by) = sb.center, tb.center
                check_inside(hwnd, ax, ay)
                check_inside(hwnd, bx, by)
                executed = f"drag #{src} ({ax},{ay}) -> #{tgt} ({bx},{by})"
                if not args.dry_run:
                    if not try_foreground(hwnd):
                        executed = "none (game not foreground at action time)"
                    else:
                        io_win.drag(hwnd, ax, ay, bx, by, duration=args.drag_s, steps=args.drag_steps)
            elif action == "drag":
                executed = "drag requested without valid target -> skipped"
            else:
                executed = "wait"
            rec["executed"] = executed

            # ---- verify by frame diff ---------------------------------------------
            changed = None
            if not args.dry_run and executed.startswith(("click", "drag")):
                time.sleep(args.post_wait)
                park_cursor()
                time.sleep(0.1)
                after = grab.grab()
                m = effect_map(frame, after, ambient)
                g = changed_frac(m)
                ls = changed_frac(m, sb) if sb.kind != "background" else 0.0
                lt = changed_frac(m, tb) if (action == "drag" and tb is not None) else 0.0
                changed = g > args.diff_global or ls > args.diff_local or lt > args.diff_local
                rec.update(post_diff_global=round(g, 5), post_diff_src=round(ls, 4), post_diff_tgt=round(lt, 4),
                           post_hash=frame_hash(after))
                print(f"           executed: {executed}; changed px global {g:.4f} src {ls:.3f}"
                      + (f" tgt {lt:.3f}" if action == "drag" else "") + f" -> {'CHANGED' if changed else 'no visible change'}")
                f = stuck.record(tick, action, sb, src_desc, changed)
                if f is not None and f.banned_until > tick:
                    print(f"           stuck: {action} '{short(src_desc, 50)}' did nothing (tried {f.count}x) -> excluded for {args.ban_ticks} ticks")
            else:
                if executed == "wait":
                    time.sleep(args.wait_s)
                print(f"           executed: {executed}{' (dry-run)' if args.dry_run else ''}")
            rec["changed"] = changed
            effect = "-" if changed is None else ("changed" if changed else "no change")
            if executed.startswith(("vetoed", "none", "drag requested")):
                effect = executed
            row["effect"] = effect

            # ---- history: one line per action (tick | state | input | element | effect)
            ssum = man.state_summary(state)
            el = f"'{short(src_desc, 60)}'" if sb else "-"
            eff = "not verified (dry-run)" if changed is None else ("changed" if changed else "NO change")
            if veto:
                history.append(f"t{tick} | {ssum} | vetoed | {el} | {veto}")
            elif action == "click":
                history.append(f"t{tick} | {ssum} | click | {el} | {eff}")
            elif action == "drag":
                tl = f"'{short(desc.get(tgt, 'nothing'), 50)}'"
                history.append(f"t{tick} | {ssum} | drag | {el} -> {tl} | {eff}")
            else:
                history.append(f"t{tick} | {ssum} | wait | - | -")
            last_state = state

            # ---- log -----------------------------------------------------------------
            stem = os.path.join(run_dir, f"tick_{tick:04d}")
            cv2.imwrite(stem + ".png", annotated)
            if args.save_raw:
                cv2.imwrite(os.path.join(run_dir, f"raw_{tick:04d}.png"), frame)
            with open(stem + ".json", "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=1)
            if overlay is not None:
                try:
                    overlay.render(annotated, idmap, res, os.path.join(run_dir, f"viz_{tick:04d}.png"),
                                   descriptions=desc, executed=executed, effect=effect, state_line=sline,
                                   chosen=(action, src, tgt), note=D["note"])
                except Exception as e:
                    print(f"[loop] overlay failed: {e}")
    finally:
        grab.close()
        write_summary(run_dir, rows, {
            "ticks": len(rows), "TOD calls": tod.total_calls, "cost": f"${tod.total_cost:.4f}",
            "dry_run": args.dry_run, "args": " ".join(sys.argv[1:]),
            "last state": state_line(last_state) if last_state else "-",
        })
        print(f"[loop] done. TOD calls={tod.total_calls} cost=${tod.total_cost:.4f}. Logs: {run_dir}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="TOD Set-of-Mark agent loop for Papers, Please")
    ap.add_argument("--max-ticks", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true", help="do everything except mouse input")
    ap.add_argument("--max-marks", type=int, default=25, help="element cap (soft: text, page corners and "
                    "detector-labelled objects are never dropped; drop-target regions come on top)")
    ap.add_argument("--send-format", choices=("png", "jpeg"), default="png", help="png: smaller than jpeg on pixel art (~80 vs ~170 KB)")
    ap.add_argument("--jpeg-quality", type=int, default=90)
    ap.add_argument("--history", type=int, default=30, help="past actions listed in the request-2 text")
    ap.add_argument("--frames", nargs="+", help="offline: run on saved frames (no game window, no input)")
    ap.add_argument("--out", default=None, help="offline: output directory")
    ap.add_argument("--send-width", type=int, default=1140, help="downscale annotated frame to this width for TOD (0=full)")
    ap.add_argument("--settle", type=float, default=0.12, help="seconds between hover/down/up so Unity sees separate frames")
    ap.add_argument("--post-wait", type=float, default=0.8, help="seconds after input before verify grab")
    ap.add_argument("--wait-s", type=float, default=1.0, help="sleep when TOD chooses wait")
    ap.add_argument("--drag-s", type=float, default=0.45, help="drag duration")
    ap.add_argument("--drag-steps", type=int, default=24, help="interpolated moves per drag")
    ap.add_argument("--diff-global", type=float, default=0.008, help="changed-pixel fraction (whole frame) counting as an effect")
    ap.add_argument("--diff-local", type=float, default=0.02, help="changed-pixel fraction inside the source/target box counting as an effect")
    ap.add_argument("--stable-thresh", type=float, default=0.01, help="changed fraction over 150 ms below which the screen counts as still")
    ap.add_argument("--max-anim-wait", type=float, default=3.0)
    ap.add_argument("--stuck-limit", type=int, default=2, help="no-effect tries before an element is excluded")
    ap.add_argument("--ban-ticks", type=int, default=6, help="ticks an ineffective element stays excluded")
    ap.add_argument("--park", type=float, nargs=2, default=(0.999, 0.003),
                    help="cursor park point as client fractions (default: top-right corner, away from controls)")
    ap.add_argument("--no-viz", dest="viz", action="store_false")
    ap.add_argument("--save-raw", action="store_true")
    args = ap.parse_args(argv)
    if args.frames:
        return offline(args)
    try:
        return run(args)
    except AbortSafety as e:
        print(f"[loop] SAFETY ABORT: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
