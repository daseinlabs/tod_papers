"""loop.py -- the TOD Set-of-Mark agent loop.

Each tick:  wait-until-stable grab
            -> extract -> REQUEST 1 (state, unmarked frame + desk OCR text): screen, day, the
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
if os.environ.get("TOD_EXTRACT_URL"):   # cloud L4 extraction (docs/remote_extraction.md)
    from . import extract_remote as _exr
    ex.extract, ex.warmup = _exr.extract, _exr.warmup
from .extract import Box
from . import layout
from . import manual as man
from .som import annotate
from .tod_client import TodClient, TodCreditExhausted, TodUnreachable, choice, noul

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
    d = ex.describe(b, W, H)
    t = b.text or ""
    if b.caption == "tab at screen edge" and b.center[0] > 0.9 * W and 0.35 * H < b.center[1] < 0.65 * H:
        # run 091053 t10-20: tray closed, passport open on the desk, TOD re-dragged the passport for 11 ticks
        return ("stamp tray tab (the stamp tray is CLOSED; drag this tab left onto the desk to open the stamp "
                "tray) -- " + d)
    return d


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

    def ban(self, tick: int, action: str, b: Box, desc: str, count: int) -> "_Fail":
        """Exclude an element now (repeat-drag rule: it 'changed' pixels but not the state)."""
        f = self._find(action, b)
        if f is None:
            f = _Fail(action, b, desc)
            self.fails.append(f)
        f.box, f.desc, f.last_tick, f.count = b, desc, tick, max(f.count, count)
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
    "Which in-game day is it? In the booth, read the small date readout in the drawer row below the counter shelf "
    "(left side, short digits like 82.11.24); on other screens read the date on the bulletin, newspaper or "
    "day-end screen. 82.11.23 / 1982.11.23 = Day 1, 82.11.24 = Day 2, 82.11.25 = Day 3.",
    {"1": "Day 1: the date reads 82.11.23 (Nov 23)", "2": "Day 2: the date reads 82.11.24 (Nov 24)",
     "3": "Day 3: the date reads 82.11.25 (Nov 25)", "later": "a later date (Nov 26 or after)",
     "unknown": "no date readable in this picture"},
)
DAY_ORDER = {"1": 1, "2": 2, "3": 3, "later": 4}


def _small_for_send(frame: np.ndarray, args) -> np.ndarray:
    H, W = frame.shape[:2]
    if args.send_width and W > args.send_width:
        return cv2.resize(frame, (args.send_width, int(H * args.send_width / W)), interpolation=cv2.INTER_AREA)
    return frame


def screen_family(frame: np.ndarray) -> tuple[str, bool]:
    """('booth'|other, tray open on the pixels) from the static layout (~8 ms, no model). Only chooses WHICH
    questions request 1 asks; every answer still comes from TOD. Agreed with TOD's booth/non-booth answer in
    284/286 ticks of runs 033604/053852/054238."""
    try:
        n = layout.to_native(frame)
        fam, _ = layout.screen_of(n)
        return fam, (bool(layout.tray_open(n)) if fam == "booth" else False)
    except Exception:
        return "booth", True


def state_probe(tod: TodClient, frame: np.ndarray, args, day: str = "unknown", inspect: tuple = man.INSPECT_KEYS,
                facts: dict | None = None, with_docs: bool = True, family: tuple | None = None):
    """REQUEST 1: plain (unmarked) frame + short text + screen, day, the manual's state questions
    (manual.state_questions, incl. the passport-under-each-stamp and passport-readable questions) and one
    identity question per paper the layout found on the desk/counter (its position + the OCR text inside it).
    Every judgement about the screen is a TOD answer here; the loop only does geometry and bookkeeping."""
    today = man.DAY_DATES.get(day, man.DAY_DATES["1"])
    q = {"screen": choice("Which kind of screen is currently shown?", dict(SCREENS)), "day": DAY_Q}
    fam, tray_px = family or ("booth", True)
    if fam != "booth":
        # menus, day-end, bulletins, cutscenes: the manual consumes only the screen kind and the date
        small = _small_for_send(frame, args)
        return tod.ask(q, text=man.STATE_TEXT, image_data_url=encode_image(small, args.send_format, args.jpeg_quality))
    if day in DAY_RULES:
        del q["day"]   # the day only changes on the day-end / bulletin screens, where it is asked
    q.update(man.state_questions(today, inspect))
    q.pop("passport_open_readable", None)   # the inspection gate also opens on open-on-desk / the paper identity
    if not tray_px:
        for k in man.STRIP_KEYS:
            q.pop(k, None)
    # TOD takes at most 16 questions per request (run 015649 t41-58: HTTP 422 with 5 doc questions + 14 state)
    docs = (facts or {}).get("docs") or []
    if (facts or {}).get("tray_open_px") is False:
        for k in man.STRIP_KEYS:   # no stamp bar on the pixels: nothing can lie under a stamp head
            q.pop(k, None)
    # lowest-value state questions give way to 2 paper identities, then a hard cap at TOD's limit
    for k in ("bulletin_or_rulebook_covering_desk", "passport_open_readable"):
        if len(q) + (min(2, len(docs)) if with_docs else 0) > TOD_MAX_Q:
            q.pop(k, None)
    for k in ("bulletin_or_rulebook_covering_desk", "passport_open_readable", "exp_month"):
        if len(q) > TOD_MAX_Q:
            q.pop(k, None)
    room = max(0, TOD_MAX_Q - len(q)) if with_docs else 0
    for i, d in enumerate(docs if with_docs else ()):
        c = DOC_CACHE.get(_doc_key(d))
        if c and (facts or {}).get("tick", 0) - c["tick"] <= DOC_CACHE_TICKS:
            d["cached"] = c   # same paper, same place, same text as a recent tick: reuse TOD's answer
        elif room > 0:
            q[f"doc{i}"] = man.doc_question(d)
            room -= 1
    small = _small_for_send(frame, args)
    # with_docs=False: sent in parallel with extraction, before the desk OCR exists (the image carries the text)
    text = man.STATE_TEXT + ("\n\n" + man.desk_text_block(facts) if with_docs else "")
    return tod.ask(q, text=text, image_data_url=encode_image(small, args.send_format, args.jpeg_quality))


def doc_probe(tod: TodClient, frame: np.ndarray, args, facts: dict):
    """REQUEST 1b (after extraction, only when a paper is not in DOC_CACHE): one identity question per new paper
    box, unmarked frame + the OCR text inside each. Returns the TOD result or None when every paper is cached."""
    q = {}
    for i, d in enumerate(facts.get("docs") or []):
        c = DOC_CACHE.get(_doc_key(d))
        if c and facts.get("tick", 0) - c["tick"] <= DOC_CACHE_TICKS:
            d["cached"] = c
        elif len(q) < TOD_MAX_Q:
            q[f"doc{i}"] = man.doc_question(d)
    if not q:
        return None
    small = _small_for_send(frame, args)
    return tod.ask(q, text=man.STATE_TEXT + "\n\n" + man.desk_text_block(facts),
                   image_data_url=encode_image(small, args.send_format, args.jpeg_quality))


def add_tod_facts(facts: dict, state: dict, df: dict, sinfo: dict | None) -> None:
    """facts += TOD's document identities, what lies under each stamp head, and the passport_under sides."""
    facts["static"] = sinfo
    facts["docs_named"] = df["docs_named"] = name_docs(state, df)
    for i, d in enumerate(df.get("docs") or []):
        a = state.get(f"doc{i}")
        if a:
            DOC_CACHE[_doc_key(d)] = {"id": a["value"], "p": a["p"], "tick": facts.get("tick", 0)}
    derive_open_on_desk(state, facts)
    facts["strip"] = strip_facts(state, df, sinfo)
    facts["passport_under"] = passport_sides(facts["strip"])
    facts["tray_open_px"] = bool((sinfo or {}).get("tray_open"))


DOC_CACHE: dict = {}   # (where, native box, OCR text) -> {"id", "p", "tick"}: an unchanged paper is not re-asked
DOC_CACHE_TICKS = 15


def _doc_key(d: dict) -> tuple:
    return d["where"], tuple(d["native"]), tuple(d.get("text") or ())


def name_docs(state: dict, df: dict) -> list[dict]:
    """df['docs'] + TOD's identity answer for each (state['doc<i>'], or the cached answer for an unchanged paper)."""
    out = []
    for i, d in enumerate(df.get("docs") or []):
        a = state.get(f"doc{i}")
        if a:
            out.append({**d, "id": a["value"], "p": a["p"]})
        elif d.get("cached"):
            out.append({**d, "id": d["cached"]["id"], "p": d["cached"]["p"]})
    return out


def derive_open_on_desk(state: dict, facts: dict) -> None:
    """Run 015649/022439: 'passport open on the desk' answered no in 49 booth ticks while the same request named a desk
    paper PASSPORT >= 0.5 -> the manual went to D2/'?'. TOD's own identity answer for a desk paper counts too."""
    best = max([d["p"] for d in facts.get("docs_named") or [] if d["where"] == "desk" and d["id"] == "passport"],
               default=0.0)
    a = state.get("document_open_on_desk")
    if best >= 0.5 and a and a["p"] < 0.5:
        state["document_open_on_desk"] = {"value": True, "p": round(best, 3), "probs": a["probs"],
                                          "derived_from": "desk paper identity = passport"}


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
               "document_open_on_desk": "open", "stamp_tray_open": "tray", "passport_open_readable": "readable", "passport_under_denied": "pD", "passport_under_approved": "pA",
               "passport_shows_stamp_mark": "mark", "bulletin_or_rulebook_covering_desk": "cover", "inspect_mode_on": "insp",
               "entry_ticket_dated_today": "ticket"}


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
    for k, ab in (("exp_year", "expY"), ("exp_month", "expM")):
        if k in state:
            bits.append(f"{ab}={state[k]['value']}:{state[k]['p']:.2f}")
    if "issuing_city" in state:
        bits.append(f"city={state['issuing_city']['value']}:{state['issuing_city']['p']:.2f}")
    if "day" in state:
        bits.append(f"day={state['day']['value']}")
    return " ".join(bits)


# --------------------------------------------------------------------------
# desk text (OCR of the documents on the desk) -> both requests
# --------------------------------------------------------------------------

def desk_facts(boxes: list, W: int, H: int, sinfo: dict | None = None) -> dict:
    """Extraction facts about the papers on the desk (geometry + OCR text only -- no judgement; TOD reads them):
    - desk_text: the OCR'd text lines lying on the desk (top to bottom), excluding text on the stamp bar;
    - docs: the static layout's paper boxes (desk + counter) with the OCR text inside each and a coarse
      position, so request 1 can ask TOD what each paper is."""
    sinfo = sinfo or {}
    sx, sy = W / layout.NATIVE_W, H / layout.NATIVE_H
    tray = bool(sinfo.get("tray_open"))
    bx1, by1, bx2, _ = layout.TRAY_BAR
    lines, seen = [], set()
    for b in sorted(boxes, key=lambda b: (b.y1, b.x1)):
        t = (b.text or "").strip()
        if not t or b.kind == "region":
            continue
        cx, cy = b.center
        if cx < 0.34 * W or cy < 0.45 * H:
            continue   # yard, booth window, counter shelf, drawer readouts
        nx, ny = cx / sx, cy / sy
        if tray and bx1 <= nx <= bx2 and ny < layout.STRIP_Y[0]:
            continue   # on the open stamp bar (stamp bodies, ALIGN VISA BENEATH STAMP label)
        if t in seen:
            continue
        seen.add(t)
        lines.append(t)
    docs = []
    for d in sinfo.get("docs") or []:
        x1, y1, x2, y2 = layout.scale_box(d["box"], W, H)
        texts = [b.text.strip() for b in sorted(boxes, key=lambda b: (b.y1, b.x1))
                 if (b.text or "").strip() and x1 <= b.center[0] <= x2 and y1 <= b.center[1] <= y2]
        docs.append({"where": d["where"], "native": list(d["box"]), "box": [x1, y1, x2, y2],
                     "text": texts[:6], "pos": ex.coarse_pos(Box(x1, y1, x2, y2, "", "panel", 1.0), W, H)})
    return {"desk_text": lines, "docs": docs[:MAX_DOC_Q],
            "tray_open_px": bool(sinfo.get("tray_open")) if "tray_open" in sinfo else None}


MAX_DOC_Q = 5   # per-document identity questions in request 1
TOD_MAX_Q = 16  # TOD API limit per request


def strip_facts(state: dict, df: dict, sinfo: dict | None) -> dict:
    """Which paper lies under each stamp head, from TOD's answers (request 1) + layout geometry:
    {'denied'|'approved': {'paper': pixel check says paper in the strip (None if unknown),
                           'passport_p': TOD P(the paper under that stamp is a passport),
                           'doc': TOD's identity of the paper overlapping that strip (or None), 'doc_p'}}."""
    sinfo = sinfo or {}
    paper = sinfo.get("passport_under") if sinfo.get("screen") == "booth" else None   # pixel 'paper here' check
    named = df.get("docs_named") or []
    out = {}
    for side, (x1, x2) in layout.STRIP_X.items():
        q = state.get(f"passport_under_{side}")
        best, bov = None, 0
        for d in named:
            if d["where"] != "desk":
                continue
            a, b, c, e = d["native"]
            ov = max(0, min(c, x2) - max(a, x1))
            if ov >= 0.4 * (x2 - x1) and b <= layout.STRIP_Y[1] and e >= layout.STRIP_Y[0] and ov > bov:
                best, bov = d, ov
        out[side] = {"paper": (side in paper) if paper is not None else None,
                     "passport_p": q["p"] if q else None,
                     "doc": best["id"] if best else None, "doc_p": best["p"] if best else None}
    return out


def passport_sides(strip: dict) -> list[str]:
    """Stamp heads with the PASSPORT under them: paper in the strip (when the pixel check ran) and TOD says the
    paper under that stamp is a passport."""
    # also when TOD's identity of the paper over that strip says passport (14/26 strip drags landed while
    # passport_under read 0.30-0.52: 005956 t8-9, 022439 t5-6/t67-76)
    return [s for s, v in strip.items()
            if v["paper"] is not False and ((v["passport_p"] or 0.0) >= 0.5
                                            or (v["paper"] and v["doc"] == "passport" and (v["doc_p"] or 0) >= 0.5))]


INSPECT_OPEN_P = 0.6
CARRY_COUNTRY_P = 0.6   # 114927 re-ask: correct readings came at 0.65-0.84; the choice has an 'unreadable' option


# --------------------------------------------------------------------------
# extractor choice: vision (extract.extract, models), static (layout.py, no models), hybrid (both)
# --------------------------------------------------------------------------


def get_boxes(frame: np.ndarray, extractor: str):
    """(boxes the loop marks, vision boxes for the desk OCR facts or None, static-layout info or None).
    hybrid = static boxes for every fixed control (horn, lever, tray tab, whole stamps, drawers) + vision
    boxes for the documents (identity, OCR text, per-paper split); drop targets then come from the static
    layout (static_regions)."""
    if extractor == "vision":
        return ex.extract(frame), None, None
    st = layout.extract_static(frame, targets=False)
    info = {**layout.LAST.get("flags", {}), "screen": layout.LAST.get("screen"),
            "docs": layout.LAST.get("docs") or []}   # paper boxes -> desk_facts -> per-document TOD questions
    if extractor == "static":
        return st, None, info
    vis = ex.extract(frame)
    return (layout.merge_hybrid(st, vis, frame_wh=(frame.shape[1], frame.shape[0]), screen=(info or {}).get("screen")),
            vis, info)


_STATIC_REGION = {   # loop region name -> layout element (native box)
    "stamp_landing_denied": "landing_denied", "stamp_landing_approved": "landing_approved",
    "tray_stow": "tray_stow", "hand_back": "hand_back", "desk": "desk", "stow_papers": "stow_papers",
}


def static_regions(frame: np.ndarray, tray_is_open: bool):
    """Drop targets from the fixed screen layout (layout.py), scaled to the frame: the two stamp landing strips
    and the tray stow edge while the tray is open (layout.tray_open on the pixels, not TOD's answer), the person
    at the window (hand back) and the desk. Returns (regions, {name: 'static'})."""
    H, W = frame.shape[:2]
    names = (["stamp_landing_denied", "stamp_landing_approved", "tray_stow"] if tray_is_open else [])
    names += ["hand_back", "desk", "stow_papers"]
    out = []
    for name in names:
        x1, y1, x2, y2 = layout.scale_box(layout.BY_NAME[_STATIC_REGION[name]].box, W, H)
        out.append(Box(x1, y1, x2, y2, "", "region", 0.0, caption=REGION_CAPS[name]))
    return out, {n: "static" for n in names}


# --------------------------------------------------------------------------
# drop-target regions (stamp landing strip, counter shelf, desk)
# --------------------------------------------------------------------------

ANCHORS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anchors.json")
_ANCHORS = None

REGION_CAPS = {
    "stamp_landing_approved": "stamp landing strip (under the APPROVED stamp head)",
    "stamp_landing_denied": "stamp landing strip (under the DENIED stamp head)",
    "counter_shelf": "counter shelf under the window",
    "hand_back": "the entrant at the booth window -- drop documents ON THE PERSON to hand them back",
    "desk": "desk (drop documents here to read them)",
    "tray_stow": "right edge of the desk (drag the tray tab here to put the stamp tray away)",
    "stow_papers": "counter shelf left of the desk -- drop the rulebook or bulletin here to put it away (it closes "
                   "and leaves the desk)",
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


def _stamp_side(b: Box, frame: np.ndarray):
    name = getattr(b, "name", "")
    if name in ("stamp_denied", "stamp_approved"):
        return name[6:]
    if b.caption in ("green rubber stamp", "red rubber stamp"):
        return "approved" if b.caption.startswith("green") else "denied"
    # a bare 'rubber stamp' box (vision extractor only) is not classified by pixel colour any more (no heuristic
    # judgements about the screen); the static layout names both stamps
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
        # --- tray stow target: right of the APPROVED stamp, at the stamp bar's height
        ab = sides.get("approved")
        r = None
        if ab:
            y1, y2 = min(b.y1 for b in ab), max(b.y2 for b in ab)
            x1 = min(W - int(0.02 * W), max(b.x2 for b in ab) + int(0.01 * W))
            r = Box(x1, y1, W - 1, y2, "", "region", 0.0, caption=REGION_CAPS["tray_stow"])
            if not (r.w >= 0.015 * W and 0.25 * H < r.center[1] < 0.65 * H):
                r = None
        if r is not None:
            src["tray_stow"] = "derived"
        else:
            r = _anchor("tray_stow", W, H)
            src["tray_stow"] = "fallback"
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
    # The counter shelf itself is NOT a hand-back target: run 092612 dropped the stamped passport on it
    # 5 times and it just lay there; dropped on the person above it, the entrant said "Thank you." and left.
    # --- hand back: the person at the window (face box, upper torso)
    hb = None
    if persons:
        p = max(persons, key=lambda b: b.area)
        cand = Box(p.x1, p.y1 + int(0.25 * p.h), p.x2, p.y2 - int(0.03 * H), "", "region", 0.0,
                   caption=REGION_CAPS["hand_back"])
        if cand.h >= 0.08 * H and cand.w >= 0.06 * W:
            hb = cand
    if hb is not None:
        src["hand_back"] = "derived"
    else:
        hb = _anchor("hand_back", W, H)
        src["hand_back"] = "fallback"
    regions.append(hb)

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


TRAY_HANDLE_CAP = "stamp tray tab (left end of the open stamp bar)"


def derive_tray_handle(boxes: list, frame: np.ndarray, state: dict):
    """While the tray is out its tab sits at the LEFT end of the stamp bar, where no
    detector draws a box. Derived from the detected DENIED stamp (the left stamp):
    a narrow box just left of it, same height. Only offered when request 1 says the
    tray is open and the DENIED stamp was detected (never a fallback)."""
    if not man.yes(state, "stamp_tray_open") or any(getattr(b, "name", "") == "tray_tab_open" for b in boxes):
        return None
    H, W = frame.shape[:2]
    den = [b for b in boxes if b.center[0] > 0.4 * W and 0.2 * H < b.center[1] < 0.7 * H
           and _stamp_side(b, frame) == "denied" and b.w > 0.08 * W]
    if not den:
        return None
    d = min(den, key=lambda b: b.x1)
    x2 = d.x1 - int(0.045 * W)
    x1 = x2 - int(0.03 * W)
    if x1 < 0.4 * W:
        return None
    return Box(x1, d.y1 + d.h // 6, x2, d.y2 - d.h // 6, "", "object", 0.0, caption=TRAY_HANDLE_CAP)


# --------------------------------------------------------------------------
# request 2 questions + the click/drag convention
# --------------------------------------------------------------------------

ACTION_RULE = (
    "Constraint: the loudspeaker/horn, the APPROVED and DENIED stamps, buttons/menu text and page corners are "
    "CLICK only; documents (passport, papers, bulletin, rulebook), the stamp tray tab and the shutter lever are "
    "DRAG only. Drop targets (stamp landing strip, counter shelf, desk) are only the end point of a drag."
)


WAIT_KEY = "wait"
WAIT_DESC = "wait - do nothing this turn (someone is walking in / a screen is changing)"


def build_questions(src_ids: dict, tgt_ids: dict, booth: bool = True) -> dict:
    """Request 2: which element (its click/drag input follows from the element, manual section 2) and, in the booth,
    which drop target. The separate action question was dropped (it cost ~1.7 s per tick with the long text; the
    convention already fixed the input for every element)."""
    q = {
        "source": choice(
            "Following the manual and what is currently true on screen: which numbered element is clicked "
            "next, or, for a drag, picked up? Use the number drawn on its marker; choose 'wait' if nothing should "
            "be done this turn. Stamping: click the stamp the passport is lying under; if you want the other "
            "decision, first drag the passport to the other strip.",
            {**src_ids, WAIT_KEY: WAIT_DESC}),
    }
    if booth:
        q["target"] = choice(
            "If the chosen element is dragged: onto which numbered drop target should it be released? (stamp "
            "landing strip = under a stamp so it can be stamped; the entrant = hand documents back; desk = read a "
            "document; tray stow edge / desk = close / open the stamp tray.) Ignored for a click.", tgt_ids)
    return q


def _build_questions_old(src_ids: dict, tgt_ids: dict) -> dict:
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
            "next, or, for a drag, picked up? Use the number drawn on its marker. Stamping: click the stamp the "
            "passport is lying under; if you want the other decision, first drag the passport to the other strip.",
            src_ids,
        ),
        "target": choice(
            "If the next input is a drag: onto which numbered drop target or element should the dragged item be "
            "released? (stamp landing strip = under a stamp so it can be stamped; counter shelf = hand documents "
            "back; desk = read a document / put papers aside / pull the tray tab left.) Ignored for a click.",
            tgt_ids,
        ),
    }


def _cls(b, booth: bool, doc: bool = False):
    if b is None:
        return None
    if b.kind == "background":
        return "click"
    if doc:
        return "drag"   # TOD's request-1 identity says this element is a paper (documents are drag-only)
    return man.input_class(b, booth)


def enforce_input(action: str, src: str, res, idmap: dict, src_ids: dict, booth: bool, doc_ids=()):
    """Apply the manual's click-only / drag-only convention to TOD's pick.
    On a conflict (e.g. drag on a stamp) take the better of
      (a) same element, its allowed input:  P(src) * P(allowed action)
      (b) same input, best element allowed for it:  P(action) * P(element)
    Returns (action, src, note); note == '' when nothing changed."""
    if action not in ("click", "drag") or not src.isdigit():
        return action, src, ""
    cls = _cls(idmap.get(int(src)), booth, int(src) in doc_ids)
    if cls in (None, action):
        return action, src, ""
    pa, ps = res["action"].probabilities, res["source"].probabilities
    opts = []
    if cls in ("click", "drag"):
        opts.append((float(ps.get(src, 0)) * float(pa.get(cls, 0)), cls, src))
    best = None
    for k in src_ids:
        if _cls(idmap.get(int(k)), booth, int(k) in doc_ids) in (None, action) and (best is None or ps.get(k, 0) > ps.get(best, 0)):
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
    lines += ["", "| tick | screen | state (TOD, request 1) | manual step | action | source desc | p(src) | effect | "
              "gt (eval only) |", "|---:|---|---|---|---|---|---:|---|---|"]
    for r in rows:
        d = str(r.get("src_desc", "-")).replace("|", "/")
        if r.get("tgt_desc"):
            d += " -> " + str(r["tgt_desc"]).replace("|", "/")
        lines.append(f"| {r['tick']} | {r.get('screen', '-')} | {r.get('state', '-')} | {r.get('step', '-')} | "
                     f"{r.get('action', '-')} | {short(d, 90)} | {r.get('p_src', 0):.2f} | {r.get('effect', '-')} | "
                     f"{r.get('gt', '-')} |")
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
            stuck: "StuckTracker | None" = None, tick: int = 0, facts: dict | None = None) -> dict:
    """Everything between (extract + request 1) and request 2: drop-target
    regions, Set-of-Mark annotation, criteria (extract.describe), questions,
    the manual text and the encoded (downscaled) image."""
    t0 = time.perf_counter()
    H, W = frame.shape[:2]
    screen = state.get("screen", {}).get("value", "")
    # a person at the window means the booth even if the screen answer drifted (counter shelf must be offered)
    booth = screen in BOOTH_SCREENS or man.yes(state, "person_at_window", 0.6)
    static = getattr(args, "extractor", "vision") != "vision"
    sinfo = (facts or {}).get("static") or {}
    if booth and static:
        regions, region_src = static_regions(frame, bool(sinfo.get("tray_open", man.yes(state, "stamp_tray_open"))))
    else:
        regions, region_src = derive_regions(boxes, frame, state) if booth else ([], {})
    if booth and facts is not None and not man.stamped(state, facts):
        # an unstamped passport is not handed back (run 054238 t62-137: 20+ unstamped hand-backs on a false mark
        # reading); the entrant target is offered once a stamp press is on record for this entrant
        regions = [r for r in regions if r.caption != REGION_CAPS["hand_back"]]
    if booth and man.yes(state, "person_at_window", HORN_HIDE_P):
        # the horn only calls someone when the window is empty (runs 114927 t38-97: 50 horn clicks with the
        # entrant standing at the window); TOD says someone is there, so it is not offered
        # the paper stores below the counter (bulletin, rulebook slot, transcript printer) are not needed on Days 1-3
        # and only add papers to the desk (run 044332 t48-52: a fresh bulletin dragged onto the stamp strips;
        # run 005956 t18-31: 14 bulletin-storage drags)
        nb = [b for b in boxes if getattr(b, "name", "") not in ("horn", "bulletin", "rulebook", "transcript")
              and b.caption != "speaker/horn"]
        if len(nb) < len(boxes) and facts is not None:
            facts["horn_hidden"] = True
        boxes = nb
    if booth and not man.yes(state, "inspect_mode_on"):
        # run 021438 t3: TOD clicked the red inspect button; inspect mode froze every drag for 8 ticks. Days 1-3 need
        # no inspect mode, so the button is offered only while TOD says inspect mode is on (to leave it)
        boxes = [b for b in boxes if getattr(b, "name", "") != "inspect_toggle"]
    handle = derive_tray_handle(boxes, frame, state) if booth else None
    if handle is not None:
        boxes = list(boxes) + [handle]
    # step E- (passport under the WRONG stamp for the carried country): the stamps are not valid sources this tick
    # (a press would be refused, run 005956 t33-35); they stay drawn, struck through, like an excluded element
    wrong = man.wrong_stamp(state, day if day in DAY_RULES else "1", facts) if booth else None
    if facts is not None:
        facts["e_minus"] = list(wrong) if wrong else None
    undecided = (not wrong and booth and man.undecided_stamp(state, day if day in DAY_RULES else "1", facts))
    if facts is not None:
        facts["e_undecided"] = bool(undecided)
    pu_now = (facts or {}).get("passport_under") or []
    if undecided:
        # E?: the page has to be read, not the tray closed (run 044332 t44: closing it started a C/E? loop)
        boxes = [b for b in boxes if not (getattr(b, "name", "") == "tray_tab_open" or b.caption == TRAY_HANDLE_CAP)]
    if wrong or undecided:
        hide_stamp = lambda b: _stamp_side(b, frame) is not None
    elif booth and facts is not None and "strip" in facts:
        # a stamp is offered only when request 1 puts the passport under it -- the same test as the refusal veto
        # (run 022439 t69-76: 5 refused presses with the passport under neither head)
        hide_stamp = lambda b: _stamp_side(b, frame) not in (None, *pu_now)
    else:
        hide_stamp = lambda b: False
    banned = ((lambda b: stuck.banned(tick, b) is not None or hide_stamp(b)) if stuck is not None else hide_stamp)
    annotated, idmap = annotate(frame, list(boxes) + regions, max_marks=args.max_marks - 1 + len(regions),
                                excluded=banned)
    if not booth:  # text/cutscene screens without a button are advanced by clicking the screen itself
        idmap[len(idmap) + 1] = Box(W // 4, H // 4, 3 * W // 4, 3 * H // 4, "", "background", 0.0)
    desc = {str(i): describe(b, W, H) for i, b in idmap.items()}
    doc_ids = set()   # element ids TOD (request 1) named as a paper -> drag-only, whatever the detector label says
    doc_of: dict = {}   # element id -> index of the paper it lies on
    for i, b in idmap.items():   # name each paper by TOD's request-1 identity answer (geometry: centre inside)
        if b.kind in ("region", "background"):
            continue
        for j, d in enumerate((facts or {}).get("docs_named") or []):
            x1, y1, x2, y2 = d["box"]
            if x1 <= b.center[0] <= x2 and y1 <= b.center[1] <= y2 and getattr(b, "name", "") not in layout.BY_NAME:
                rest = desc[str(i)].split(" — ", 1)[-1]   # drop the detector kind; TOD's identity names it
                desc[str(i)] = f"{d['id']} (TOD {d['p']:.2f}) — {rest}"
                if d["id"] != "other":
                    doc_ids.add(i)   # run 114927 t30-86: the counter passport was labelled 'rubber stamp' (click-only)
                    if b.kind != "page_corner":
                        doc_of[i] = j
                break
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
    # one drag source per paper (61% of booth ticks offered the same passport 2+ times): the largest box on it
    dup_ids = set()
    for j in set(doc_of.values()):
        els = sorted((i for i, jj in doc_of.items() if jj == j), key=lambda i: -idmap[i].area)
        dup_ids.update(str(i) for i in els[1:])
    # regions are drop targets only; the background is a click source only
    src_ids = {k: v for k, v in desc.items() if k not in banned_ids and idmap[int(k)].kind != "region"
               and not hide_stamp(idmap[int(k)]) and k not in dup_ids}
    tgt_ids = {k: v for k, v in desc.items() if idmap[int(k)].kind != "background"} or dict(desc)
    if booth:   # in the booth every drag ends on a drop-target region (manual section 3); nothing else is a target
        tgt_ids = {k: v for k, v in tgt_ids.items() if idmap[int(k)].kind == "region"} or tgt_ids
    region_ids = {k: idmap[int(k)].caption for k in desc if idmap[int(k)].kind == "region"}
    region_info = {}
    for k, cap in region_ids.items():
        name = next(n for n, c in REGION_CAPS.items() if c == cap)
        b = idmap[int(k)]
        region_info[name] = {"id": k, "box": [b.x1, b.y1, b.x2, b.y2], "target_source": region_src.get(name, "?")}
    questions = build_questions(src_ids, tgt_ids, booth)
    if facts is not None:
        facts["booth"] = booth
    state_text = man.build(state, history, day, ban_lines, facts)
    send = _small_for_send(annotated, args)
    url = encode_image(send, args.send_format, args.jpeg_quality)
    return dict(annotated=annotated, idmap=idmap, desc=desc, banned_ids=banned_ids, doc_ids=doc_ids, src_ids=src_ids,
                tgt_ids=tgt_ids, regions=region_info, booth=booth, questions=questions, state_text=state_text,
                image_url=url, image_kb=round(len(url) * 3 / 4 / 1024, 1),
                tray_flips=(facts or {}).get("tray_flips", 0), e_minus=wrong,
                prep_ms=round((time.perf_counter() - t0) * 1e3, 1))


_PROBE_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="state-probe")
_DOC_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="doc-probe")


def _timed(fn, *a):
    t = time.perf_counter()
    r = fn(*a)
    return r, (time.perf_counter() - t) * 1e3


def _clean_state(state: dict) -> dict:
    return {k: {"value": v["value"], "p": v["p"], "probs": {a: round(b, 4) for a, b in v["probs"].items()}}
            for k, v in state.items()}


TRAY_FLIP_LIMIT = man.TRAY_FLIP_LIMIT
REPEAT_DRAG_N = 4   # same drag source + same target, state summary unchanged, N ticks running -> exclude the source
HORN_HIDE_P = 0.7
HANDBACK_STAY = 4   # ticks a person may stay at the window after a hand-back before it is discounted
REPEAT_WINDOW, REPEAT_STOP = 12, 10   # same executed input 10 of the last 12 ticks -> stall stop (run 114927)    # person_at_window P(yes) above which the horn is not offered


def _is_tray_tab(b) -> bool:
    return b is not None and ("stamp tray tab" in (b.caption or "")
                              or getattr(b, "name", "") in ("tray_tab", "tray_tab_open"))


def fill_derived(res, D: dict) -> None:
    """Logs/overlay expect 'action' (and 'target') answers; request 2 no longer asks them. Insert the derived input
    (marked derived=True) so the JSON and the overlay stay readable."""
    from types import SimpleNamespace
    if "action" not in res.answers:
        res.answers["action"] = SimpleNamespace(value=D["action"], probabilities={D["action"]: 1.0}, confidence=None,
                                                qtype="derived", derived=True)
    if "target" not in res.answers:
        res.answers["target"] = SimpleNamespace(value="none", probabilities={"none": 1.0}, confidence=None,
                                                qtype="derived", derived=True)


def decide(res, P: dict) -> dict:
    """TOD's request-2 answers -> the input to perform (after the click/drag
    convention). Pure: no I/O."""
    src = str(res["source"].value)
    if src == WAIT_KEY or not src.isdigit():
        return dict(action="wait", src=src, tgt="none", note="", tod_pick=("wait", src),
                    p_src=float(res["source"].probabilities.get(src, 0.0)))
    cls = _cls(P["idmap"].get(int(src)), P["booth"], int(src) in P.get("doc_ids", ()))
    action = "drag" if cls == "drag" else "click"   # the element's own input (manual section 2)
    tod_pick = (action, src)
    action, src, note = enforce_input(action, src, res, P["idmap"], P["src_ids"], P["booth"], P.get("doc_ids", ()))
    sb0 = P["idmap"].get(int(src)) if src.isdigit() else None
    closing = sb0 is not None and ("left end of the open" in (sb0.caption or "") or getattr(sb0, "name", "") == "tray_tab_open")
    if P.get("tray_flips", 0) >= TRAY_FLIP_LIMIT and action == "drag" and _is_tray_tab(sb0) and closing:
        # only the CLOSING half of the ping-pong is skipped: opening a closed tray is the next step (C) and run
        # 044332 t43-51 sat in C because the guard replaced every opening drag with a document drag
        # run 094930/101021: TOD toggled the tray open/closed for 48 ticks; the warning in the state block did
        # not stop it. Take TOD's best other drag-able element (the documents) instead of the tab.
        ps = res["source"].probabilities
        docs = P.get("doc_ids", ())
        # papers TOD named first (run 005956 t18: the fallback re-pick was the bulletin-storage drawer, which then
        # looped 14 ticks), then any other drag-able element
        alt = [k for k in sorted(P["src_ids"], key=lambda k: (int(k) not in docs, -float(ps.get(k, 0))))
               if not _is_tray_tab(P["idmap"].get(int(k)))
               and _cls(P["idmap"].get(int(k)), P["booth"], int(k) in docs) == "drag"]
        if alt:
            note = (note + "; " if note else "") + (f"tray toggled {P['tray_flips']}x without a stamp -> "
                                                    f"tab #{src} skipped, re-picked #{alt[0]}")
            src = alt[0]
    if action == "drag" and "target" not in res.answers:
        return dict(action="wait", src=src, tgt="none", note="drag-only element off the booth -> wait",
                    tod_pick=tod_pick, p_src=float(res["source"].probabilities.get(src, 0.0)))
    tgt = str(res["target"].value) if "target" in res.answers else "none"
    sb = P["idmap"].get(int(src)) if src.isdigit() else None
    if action == "drag" and _is_tray_tab(sb):
        # the tab only toggles the tray: an open tray's tab goes RIGHT onto the stow edge, a closed tray's tab LEFT
        # onto the desk. Compare dry-run 2026-10-02 (003519_0033, loop5 live): TOD picked the open tab but dropped
        # it on a stamp landing strip, where it does nothing.
        is_open = "left end of the open" in (sb.caption or "") or getattr(sb, "name", "") == "tray_tab_open"
        want = REGION_CAPS["tray_stow"] if is_open else REGION_CAPS["desk"]
        ok = [k for k, b in P["idmap"].items() if b.kind == "region" and b.caption == want]
        if ok and tgt != str(ok[0]):
            note = (note + "; " if note else "") + (f"tray tab dragged onto #{tgt} -> #{ok[0]} "
                                                    f"({'stow edge' if is_open else 'desk'})")
            tgt = str(ok[0])
    tb_ = P["idmap"].get(int(tgt)) if tgt.isdigit() else None
    if (action == "drag" and sb is not None and tb_ is not None and tb_.kind == "region"
            and tb_.caption == REGION_CAPS["desk"] and not _is_tray_tab(sb)
            and tb_.x1 <= sb.center[0] <= tb_.x2 and tb_.y1 <= sb.center[1] <= tb_.y2):
        # the paper already lies on the desk target: dropping it there again is a no-op (coordinator count: 26
        # re-drops in runs 015649/022439) -> TOD's best other target
        alt = [k for k, _ in sorted(res["target"].probabilities.items(), key=lambda kv: -kv[1])
               if k != tgt and k != src and k.isdigit() and P["idmap"].get(int(k)) is not None
               and P["idmap"][int(k)].caption != REGION_CAPS["hand_back"]]   # never an unplanned hand-back
        if alt:
            note = (note + "; " if note else "") + f"#{src} already lies on the desk target -> #{alt[0]}"
            tgt = alt[0]
    if action == "drag" and tgt == src:
        # dropping an item on itself is a no-op: take TOD's best other target
        alt = [k for k, _ in sorted(res["target"].probabilities.items(), key=lambda kv: -kv[1]) if k != src]
        if alt:
            tgt = alt[0]
    return dict(action=action, src=src, tgt=tgt, note=note, tod_pick=tod_pick,
                p_src=float(res["source"].probabilities.get(src, 0.0)))


@dataclass
class Entrant:
    """What the loop remembers about the entrant being processed (fed back to TOD
    as facts in the state block and the history block; TOD still decides):
    the most recent confident issuing-country reading, stamp clicks that changed
    the screen while the passport lay under the stamp heads, and the hand-back."""
    country: dict | None = None          # {"value", "p", "tick"}
    stamp_clicks: list = field(default_factory=list)   # [(tick, 'approved'|'denied')]
    handed_back: int | None = None
    started: int = 0
    last_under: list = field(default_factory=list)   # stamp heads the passport lay under this tick
    tray_seen: list = field(default_factory=list)    # [(tick, tray_open)] -- open/close oscillation check
    missed_stamps: list = field(default_factory=list)  # [(tick, side)] stamp pressed, passport under the other head
    hb_drop: int | None = None   # tick of the last document drag onto the person that changed the screen
    city: dict | None = None                     # {"value", "p", "tick"} issuing city reading (Day 2+)
    exp: dict | None = None                      # {"year", "month", "p", "tick"} EXP. reading (Day 2+)
    checks: dict = field(default_factory=dict)   # {check key: {"value", "p", "tick"}} confident yes/no readings
    log: list = field(default_factory=list)          # [(tick, why)] every reset (for the run report)

    def reset(self, tick: int, why: str) -> None:
        self.country, self.stamp_clicks, self.handed_back, self.started = None, [], None, tick
        self.hb_drop, self.missed_stamps, self.checks, self.city, self.exp = None, [], {}, None, None
        self.tray_seen = []   # run 005956 t18/t21: entrant 2's tray toggles blocked entrant 3's first tray opening
        self.log.append((tick, why))
        print(f"           entrant memory reset ({why})")

    def observe(self, tick: int, state: dict) -> None:
        """Start-of-tick update from request 1."""
        person = man.yes(state, "person_at_window")
        if self.hb_drop is not None and tick - self.hb_drop <= 3 and not person:
            # a document was dropped on the person and the person is gone now: they took it and left,
            # whether or not a stamp was detected (run 101448: the stamp was missed, memory carried over)
            self.reset(tick, f"hand-back drop at tick {self.hb_drop} + person gone")
        elif self.hb_drop is not None and tick - self.hb_drop > 3:
            self.hb_drop = None   # the person stayed: the drop was not a hand-back
        elif self.handed_back is not None and person and tick - self.handed_back > HANDBACK_STAY:
            # the person is still at the window long after the 'hand-back': it was not one (run 114927 t36: the
            # transcript printer was dragged onto the person; the state block then said 'finished' for 60 ticks)
            self.reset(tick, f"person still at the window {tick - self.handed_back} ticks after the hand-back at "
                             f"tick {self.handed_back}: not a hand-back")
        elif not person and (self.handed_back is not None or not (
                man.yes(state, "document_open_on_desk") or man.yes(state, "document_on_counter_shelf"))):
            if self.country or self.stamp_clicks or self.handed_back is not None:
                self.reset(tick, "window empty")
        self.tray_seen = (self.tray_seen + [(tick, man.yes(state, "stamp_tray_open"))])[-9:]
        c = state.get("issuing_country")
        if (c and c["p"] >= CARRY_COUNTRY_P and c["value"] != "unreadable"
                and (self.country is None or c["p"] >= self.country["p"] or c["value"] == self.country["value"])):
            self.country = {"value": c["value"], "p": c["p"], "tick": tick}
        ci = state.get("issuing_city")
        if (ci and ci["p"] >= (man.DENY_P if ci["value"] == "other" else CARRY_COUNTRY_P) and ci["value"] != "unreadable"
                and (self.city is None or ci["p"] >= self.city["p"] or ci["value"] == self.city["value"])):
            self.city = {"value": ci["value"], "p": ci["p"], "tick": tick}
        y, m = state.get("exp_year"), state.get("exp_month")
        if y and y["value"] != "unreadable" and y["p"] >= 0.5 and (self.exp is None or y["p"] >= self.exp["p"]):
            mm = m["value"] if m and m["value"] != "unreadable" and m["p"] >= 0.5 else None
            self.exp = {"year": y["value"], "month": mm, "p": y["p"], "tick": tick}
        for k in man.CHECK_KEYS:   # Day 2/3 checks, carried like the country (the page is hidden once under a stamp)
            v = man.check_answer(state.get(k))
            if v is not None:
                self.checks[k] = {"value": v, "p": state[k]["p"], "tick": tick}

    def after_action(self, tick: int, state: dict, action: str, sb, tb, frame, changed) -> None:
        if not changed or sb is None:
            return
        if action == "click":
            side = _stamp_side(sb, frame)
            if side and self.handed_back is not None:
                self.reset(tick, f"stamp pressed after the hand-back at tick {self.handed_back}: a new entrant")
            if side:
                # any click on a stamp (knob or body) that changed the screen is a stamp press (run 101448 t11:
                # the knob click stamped the passport but was not recorded). Only when the strip check says the
                # passport lies under the OTHER head only is it logged as a press that marked nothing.
                under = self.last_under or []
                if under and side not in under:
                    self.missed_stamps.append((tick, side))
                else:
                    self.stamp_clicks.append((tick, side))
            elif ((sb.caption == "speaker/horn" or getattr(sb, "name", "") == "horn")
                  and self.handed_back is not None and not man.yes(state, "person_at_window")):
                self.reset(tick, "horn clicked after hand-back")
        elif (action == "drag" and tb is not None and tb.kind == "region"
              and tb.caption == REGION_CAPS["hand_back"]   # the drag passed the click/drag convention
              and sb.caption not in (TRAY_HANDLE_CAP, "tab at screen edge", "lever handle")
              and getattr(sb, "name", "") not in layout.BY_NAME):   # a paper, not a booth fixture (114927 t36)
            if self.stamp_clicks or man.yes(state, "passport_shows_stamp_mark"):
                # a stamped passport dropped on the person and the frame changed: this entrant is done. Reset
                # now (the next entrant must not inherit the country/stamps) but keep handed_back so the
                # manual says "wait for them to leave, then click the horn".
                done = (f"hand-back drop at tick {tick} (country={(self.country or {}).get('value')}, "
                        f"stamps={[sd for _, sd in self.stamp_clicks] or 'mark only'})")
                self.reset(tick, done)
                self.handed_back = tick
            else:
                self.hb_drop = tick   # unstamped: reset only if the person then leaves (observe)

    def facts(self, tick: int, df: dict) -> dict:
        return {**df, "tick": tick, "country_carried": self.country, "stamp_clicks": list(self.stamp_clicks),
                "missed_stamps": list(self.missed_stamps), "handed_back": self.handed_back, "tray_flips": self.tray_flips(),
                "checks_carried": dict(self.checks), "city_carried": self.city, "exp_carried": self.exp}

    def tray_flips(self) -> int:
        """Open<->closed changes of the stamp tray over the last 8 ticks with no stamp click in between.
        Run 094930 t10-57: open, close, open, close ... 48 ticks (each toggle 'changed', so no stall stop)."""
        last_stamp = self.stamp_clicks[-1][0] if self.stamp_clicks else -1
        seq = [o for t, o in self.tray_seen if t > last_stamp]
        return sum(1 for a, b in zip(seq, seq[1:]) if a != b)


def gate_inspection(state: dict, asked: tuple, df: dict | None = None) -> dict:
    """Drop inspection answers unless request 1 also says a passport lies open on the desk (document_open_on_desk)
    or its data page is readable (passport_open_readable), p >= INSPECT_OPEN_P. The country choice has its own
    'unreadable' option. (114927 t13/t14: the open Impor passport half under the tray got readable=0.10,
    open=0.62 and IMPOR 0.65-0.72 -- the readable-only gate dropped a correct reading.) Returns the dropped
    answers (logged only)."""
    dropped = {}
    gate_p = max(state.get("passport_open_readable", {}).get("p", 0.0),
                 state.get("document_open_on_desk", {}).get("p", 0.0))
    for i, d in enumerate((df or {}).get("docs") or []):   # TOD named a desk paper the passport (loop7 dryrun10:
        a = state.get(f"doc{i}")                           # open-on-desk said no, every inspection answer dropped)
        if a is None and d.get("cached"):
            a = {"value": d["cached"]["id"], "p": d["cached"]["p"]}
        if a and d["where"] == "desk" and a["value"] == "passport":
            gate_p = max(gate_p, a["p"])
    if asked and gate_p < INSPECT_OPEN_P:
        for k in man.INSPECT_KEYS:
            if k in state:
                dropped[k] = state.pop(k)
    return dropped


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
        boxes, vis, sinfo = get_boxes(frame, args.extractor)
        t_ex = (time.perf_counter() - t0) * 1e3
        df = desk_facts(boxes, frame.shape[1], frame.shape[0], sinfo)
        DOC_CACHE.clear()   # offline frames are unrelated pictures
        oday = args.day or "unknown"   # offline only: the day the frames come from (live runs read it via request 1)
        asked = ("issuing_country",) if oday == "1" else man.INSPECT_KEYS
        probe, t_probe = _timed(state_probe, tod, frame, args, oday, asked, df)
        state = parse_state(probe)
        gate_inspection(state, asked, df)
        ent = Entrant()
        ent.observe(0, state)
        facts = ent.facts(0, df)
        add_tod_facts(facts, state, df, sinfo)
        P = prepare(frame, boxes, state, deque(), oday, args, facts=facts)
        t1 = time.perf_counter()
        res = tod.ask(P["questions"], text=P["state_text"], image_data_url=P["image_url"])
        t_tod = (time.perf_counter() - t1) * 1e3
        D = decide(res, P)
        fill_derived(res, D)
        step = man.situation(state, oday if oday in DAY_RULES else "1", facts)
        r = {
            "frame": path, "extractor": args.extractor, "passport_under": facts.get("passport_under"),
            "n_boxes_raw": len(boxes), "n_sources": len(P["src_ids"]), "n_targets": len(P["tgt_ids"]),
            "extract_ms": round(t_ex), "state_ms": round(t_probe), "tod_ms": round(t_tod),
            "image_kb": P["image_kb"], "state": _clean_state(state), "state_line": state_line(state),
            "inspect_asked": list(asked), "desk_facts": df,
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


_GT = None


def _gt_snapshot() -> dict:
    """tod_papers.gt label for the tick JSON (eval only). Never raises; {'ok': False} when unavailable."""
    global _GT
    if _GT is None:
        try:
            from . import gt as _g
            _GT = _g
        except Exception as e:   # pymem missing etc.
            _GT = False
            print(f"[loop] gt: unavailable ({e})")
    if not _GT:
        return {"ok": False, "error": "gt unavailable"}
    try:
        s = _GT.snapshot()
        e = s.get("entrant") or {}
        return {k: s.get(k) for k in ("ok", "screen", "day", "clock", "day_processed", "num_citations", "savings")} | {
            "entrant": e.get("name"), "correct": e.get("correct_verdict"), "given": e.get("given_verdict"),
            "errors": e.get("noticeable_errors")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _raise_priority() -> None:
    """Above-normal priority for this process only. Run 033308: with the game in the foreground the background loop
    process got OCR times of 10-25 s per tick (offline 1.5-2 s); AboveNormal brought them to 2.5-6.6 s."""
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = wintypes.HANDLE      # pseudo-handle -1: must not be truncated to 32 bits
        k.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        if not k.SetPriorityClass(k.GetCurrentProcess(), 0x8000):   # ABOVE_NORMAL_PRIORITY_CLASS
            print(f"[loop] SetPriorityClass failed (error {k.GetLastError()})")
    except Exception as e:
        print(f"[loop] priority not raised: {e}")


def run(args) -> int:
    _raise_priority()
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

    stop_screens = {s.strip() for s in (args.stop_on_screen or "").split(",") if s.strip()}
    stop_run = 0
    stop_reason = None
    stall_key, stall_n = None, 0
    screen_seq: list[str] = []
    recent_inputs: deque = deque(maxlen=REPEAT_WINDOW)
    menu_bounces = 0
    ent = Entrant()
    pick_key, pick_n, refused_n = None, 0, 0   # --pick-stop / --refuse-stop counters (per entrant)
    drag_key, drag_n = None, 0                  # repeat-drag rule (REPEAT_DRAG_N)
    n_resets = 0                                # len(ent.log) last seen -> entrant-scoped counters reset
    unreach_n = 0                               # --unreachable-stop counter (consecutive)
    bad4xx_n = 0                                # consecutive TOD 4xx (bad request) ticks -> stop at 2

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
            # if another window holds the foreground, wait (no input, no tick spent) instead of aborting
            fg_misses = 0
            while not try_foreground(hwnd):
                fg_misses += 1
                print(f"[tick {tick:03d}] game is not foreground ({fg_misses}x); waiting, not acting")
                if fg_misses >= args.fg_patience:
                    row.update(action="none", effect="skipped: game not foreground")
                    raise AbortSafety(f"game window lost foreground {fg_misses} times in a row")
                time.sleep(2.0)
            moved = io_win.ensure_onscreen(hwnd)
            if moved is not None:
                print(f"[tick {tick:03d}] game client was partly off-screen; moved it to {moved}")
                rec["window_moved_to"] = list(moved)
                grab.close()
                grab = io_win.Grabber(hwnd)
            park_cursor()
            time.sleep(0.1)
            frame, waited, af, stable, ambient = wait_stable(grab.grab, interval=0.15, thresh=args.stable_thresh,
                                                             max_wait=args.max_anim_wait)
            rec["gt"] = _gt_snapshot()   # ground truth from game memory: logs/eval ONLY, never in any TOD text
            _g = rec["gt"]
            row["gt"] = (f"d{_g.get('day')} {_g.get('clock')} proc={_g.get('day_processed')} cit={_g.get('num_citations')}"
                         f" {(_g.get('entrant') or '-')[:14]} correct={_g.get('correct')} given={_g.get('given')}"
                         if _g.get("ok") else "-")
            rec.update(frame_shape=list(frame.shape), frame_hash=frame_hash(frame),
                       anim_wait_s=round(waited, 2), anim_frac=round(af, 5), stable=stable)
            if not stable:
                print(f"[tick {tick:03d}] screen still animating after {waited:.1f}s (frac {af:.4f}); proceeding anyway")

            # ---- REQUEST 1 (state, unmarked frame) in parallel with extraction; then 1b (new papers only) -----
            # Day 1 decides on the country only; expiry/photo are asked from Day 2 (frees 2 of TOD's 16 questions)
            asked = ("issuing_country",) if day == "1" else man.INSPECT_KEYS
            fam = screen_family(frame)
            rec["screen_family"] = list(fam)
            fut = _PROBE_POOL.submit(_timed, state_probe, tod, frame, args, day, asked, {"tick": tick}, False, fam)
            t0 = time.perf_counter()
            boxes, vis, sinfo = get_boxes(frame, args.extractor)
            rec["extract_ms"] = round((time.perf_counter() - t0) * 1e3, 1)
            rec["extractor"] = args.extractor
            rec["extract_timings"] = {k: round(v, 1) for k, v in getattr(ex, "LAST_TIMINGS", {}).items()}
            if sinfo is not None:
                rec["static_layout"] = {k: v for k, v in sinfo.items() if k in (
                    "screen", "tray_open", "passport_under", "ms")}
            df = desk_facts(boxes, frame.shape[1], frame.shape[0], sinfo)
            df["tick"] = tick
            rec["desk_facts"], rec["inspect_asked"] = df, list(asked)
            try:
                dfut = _DOC_POOL.submit(_timed, doc_probe, tod, frame, args, df) if fam[0] == "booth" else None
                probe, rec["state_ms"] = fut.result()
                state = parse_state(probe)
                dres, rec["doc_ms"] = dfut.result() if dfut is not None else (None, 0.0)
                if dres is not None:
                    state.update(parse_state(dres))
                rec["docs_cached"] = sum(1 for d in df.get("docs") or [] if d.get("cached"))
            except TodCreditExhausted as e:  # 402: every later call fails too -> stop once, do not skip ticks
                stop_reason = "TOD credit exhausted (402)"
                print(f"[tick {tick:03d}] {e}")
                print(f"[loop] STOP: {stop_reason}")
                rec.update(tod_error=f"state: {e}", executed="none (TOD credit exhausted)", stop_reason=stop_reason)
                row.update(action="none", effect="stop: " + stop_reason)
                with open(os.path.join(run_dir, f"tick_{tick:04d}.json"), "w", encoding="utf-8") as fh:
                    json.dump(rec, fh, indent=1)
                break
            except RuntimeError as e:  # never act on a stale picture of the screen
                print(f"[tick {tick:03d}] state request failed: {e}; skipping tick")
                rec.update(tod_error=f"state: {e}", executed="none (state request error)")
                row.update(action="none", effect="skipped: state request error")
                unreach_n = unreach_n + 1 if isinstance(e, TodUnreachable) else 0
                bad4xx_n = bad4xx_n + 1 if "TOD HTTP 4" in str(e) else 0
                if bad4xx_n >= 2:   # run 015649 t41-58: the same 422 repeated 17 ticks; a bad request will not fix itself
                    stop_reason = f"TOD rejected the request twice in a row ({str(e)[:80]})"
                    print(f"[loop] STOP: {stop_reason}")
                    rec["stop_reason"] = stop_reason
                    row["effect"] = "stop: " + stop_reason
                if args.unreachable_stop and unreach_n >= args.unreachable_stop:
                    stop_reason = "TOD unreachable"
                    print(f"[loop] STOP: {stop_reason} ({unreach_n} ticks in a row)")
                    rec["stop_reason"] = stop_reason
                    row["effect"] = "stop: " + stop_reason
                with open(os.path.join(run_dir, f"tick_{tick:04d}.json"), "w", encoding="utf-8") as fh:
                    json.dump(rec, fh, indent=1)
                if stop_reason:
                    break
                time.sleep(2.0)
                continue
            dropped = gate_inspection(state, asked, df)
            if dropped:
                rec["inspect_dropped (open<0.6)"] = _clean_state(dropped)
            screen = state["screen"]["value"]
            dv = state.get("day", {})
            if dv.get("value") in DAY_RULES and dv.get("p", 0) >= 0.5 and (
                    day not in DAY_ORDER or DAY_ORDER[dv["value"]] >= DAY_ORDER[day]):
                day = dv["value"]   # days only move forward (run 015649 t46-52: Day 2 booth frames read '1' at 0.45)
            ent.observe(tick, state)
            if len(ent.log) != n_resets:   # new entrant: stall / pick / refusal counters are entrant-scoped
                n_resets = len(ent.log)
                refused_n, pick_key, pick_n, stall_key, stall_n, drag_key, drag_n = 0, None, 0, None, 0, None, 0
            facts = ent.facts(tick, df)
            add_tod_facts(facts, state, df, sinfo)
            rec["e_minus"] = facts.get("e_minus")
            rec["docs_named"] = [{k: d[k] for k in ("where", "id", "p", "text")} for d in facts["docs_named"]]
            rec["strip"] = facts["strip"]
            rec["entrant"] = {"country": ent.country, "city": ent.city, "exp": ent.exp, "checks": dict(ent.checks), "stamp_clicks": list(ent.stamp_clicks),
                              "missed_stamps": list(ent.missed_stamps), "handed_back": ent.handed_back}
            step = man.situation(state, day if day in DAY_RULES else "1", facts)
            sline = state_line(state)
            rec.update(state=_clean_state(state), state_line=sline,
                       manual_step_for_state=list(step))  # diagnostic only, never sent to TOD

            # ---- harness stop conditions (no input is sent on a stopping tick) -------
            if stop_screens:
                stop_run = stop_run + 1 if screen in stop_screens else 0
                if stop_run >= args.stop_consecutive:
                    stop_reason = f"screen in {sorted(stop_screens)} for {stop_run} consecutive ticks"
            key = (screen, man.state_summary(state, facts))
            stall_key, stall_n = key, (stall_n + 1 if key == stall_key else 1)
            if args.stall_stop and stall_n >= args.stall_stop:
                stop_reason = f"stalled: same state {key} for {stall_n} ticks"
            if stop_reason:
                print(f"[tick {tick:03d}] state: {sline}")
                print(f"[loop] STOP: {stop_reason}")
                rec.update(executed="none (stop condition)", stop_reason=stop_reason)
                row.update(screen=screen, state=man.state_summary(state, facts), step=step[0], action="none",
                           effect="stop: " + stop_reason)
                cv2.imwrite(os.path.join(run_dir, f"raw_{tick:04d}.png"), frame)
                with open(os.path.join(run_dir, f"tick_{tick:04d}.json"), "w", encoding="utf-8") as fh:
                    json.dump(rec, fh, indent=1)
                break

            # ---- menu <-> day-select bouncing (run 20261002_113830: 15 ticks of STORY, BACK, STORY ...) ------
            if not screen_seq or screen_seq[-1] != screen:
                if screen_seq and screen_seq[-1] == "day_select" and screen == "menu":
                    menu_bounces += 1
                screen_seq.append(screen)
            facts["menu_bounces"] = menu_bounces

            # ---- REQUEST 2 (action, SoM frame) -------------------------------------
            P = prepare(frame, boxes, state, history, day, args, stuck=stuck, tick=tick, facts=facts)
            annotated, idmap, desc, banned_ids = P["annotated"], P["idmap"], P["desc"], P["banned_ids"]
            rec["prep_ms"] = P["prep_ms"]
            rec["regions"] = P["regions"]
            rec["target_source"] = {n: v["target_source"] for n, v in P["regions"].items()}
            t1 = time.perf_counter()
            try:
                res = tod.ask(P["questions"], text=P["state_text"], image_data_url=P["image_url"])
            except TodCreditExhausted as e:
                stop_reason = "TOD credit exhausted (402)"
                print(f"[tick {tick:03d}] {e}")
                print(f"[loop] STOP: {stop_reason}")
                rec.update(tod_error=str(e), executed="none (TOD credit exhausted)", stop_reason=stop_reason)
                row.update(action="none", effect="stop: " + stop_reason)
                with open(os.path.join(run_dir, f"tick_{tick:04d}.json"), "w", encoding="utf-8") as fh:
                    json.dump(rec, fh, indent=1)
                break
            except RuntimeError as e:  # network/HTTP failure: log, skip the tick, never act blind
                print(f"[tick {tick:03d}] TOD request failed: {e}; skipping tick")
                rec.update(tod_error=str(e), executed="none (TOD error)")
                row.update(action="none", effect="skipped: TOD error")
                unreach_n = unreach_n + 1 if isinstance(e, TodUnreachable) else 0
                bad4xx_n = bad4xx_n + 1 if "TOD HTTP 4" in str(e) else 0
                if bad4xx_n >= 2:   # run 015649 t41-58: the same 422 repeated 17 ticks; a bad request will not fix itself
                    stop_reason = f"TOD rejected the request twice in a row ({str(e)[:80]})"
                    print(f"[loop] STOP: {stop_reason}")
                    rec["stop_reason"] = stop_reason
                    row["effect"] = "stop: " + stop_reason
                if args.unreachable_stop and unreach_n >= args.unreachable_stop:
                    stop_reason = "TOD unreachable"
                    print(f"[loop] STOP: {stop_reason} ({unreach_n} ticks in a row)")
                    rec["stop_reason"] = stop_reason
                    row["effect"] = "stop: " + stop_reason
                with open(os.path.join(run_dir, f"tick_{tick:04d}.json"), "w", encoding="utf-8") as fh:
                    json.dump(rec, fh, indent=1)
                if stop_reason:
                    break
                time.sleep(2.0)
                continue
            rec["tod_ms"] = round((time.perf_counter() - t1) * 1e3, 1)
            unreach_n = bad4xx_n = 0
            rec["tod_request_id"] = res.request_id
            res.answers["screen"] = probe["screen"]  # overlay shows it alongside the other answers

            D = decide(res, P)
            fill_derived(res, D)
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
            row.update(screen=screen, state=man.state_summary(state, facts), step=step[0], action=action,
                       src_desc=src_desc, p_src=p_src)
            if action == "drag":
                row["tgt_desc"] = desc.get(tgt, tgt)

            # ---- safety gates ----------------------------------------------------
            veto = None
            if action in ("click", "drag") and sb is not None and screen in ("day_select", "menu") \
                    and RISKY_RE.search(src_desc) and p_src <= 0.9:
                veto = f"declined '{short(src_desc, 50)}' on {screen} (destructive-looking, p={p_src:.2f} <= 0.9)"
            side = _stamp_side(sb, frame) if (action == "click" and sb is not None) else None
            if side and side not in (facts.get("passport_under") or []):
                # run 114927 t37-94: 9 DENIED presses on the RULEBOOK lying under the strip. A stamp press is executed
                # only when TOD's request-1 answer says the paper under THAT stamp is the passport.
                veto = "refused: " + man.under_phrase(facts, side)
                print(f"           {veto}")
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
                park_cursor()

                def _effect():
                    after_ = grab.grab()
                    m = effect_map(frame, after_, ambient)
                    g_ = changed_frac(m)
                    ls_ = changed_frac(m, sb) if sb.kind != "background" else 0.0
                    lt_ = changed_frac(m, tb) if (action == "drag" and tb is not None) else 0.0
                    if action == "drag":
                        # a drag only counts when the source or the drop target changed (run 005956 t23-30: the
                        # bulletin-storage drag moved nothing, src/tgt 0.000, but yard motion gave global 0.010)
                        ch_ = ls_ > args.diff_local or lt_ > args.diff_local
                    else:
                        ch_ = g_ > args.diff_global or ls_ > args.diff_local or lt_ > args.diff_local
                    return after_, g_, ls_, lt_, ch_
                # early check, then the full post-wait only when nothing has changed yet (a seen change is final)
                time.sleep(min(args.post_wait, args.post_wait_early))
                after, g, ls, lt, changed = _effect()
                waited_post = min(args.post_wait, args.post_wait_early)
                if not changed and args.post_wait > args.post_wait_early:
                    time.sleep(args.post_wait - args.post_wait_early)
                    after, g, ls, lt, changed = _effect()
                    waited_post = args.post_wait
                rec["post_wait_s"] = waited_post
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
            ssum = man.state_summary(state, facts)
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
            if action == "drag" and sb is not None and tb is not None and not veto and executed.startswith("drag"):
                dk = (src_desc, tb.caption or desc.get(tgt, ""), ssum)
                same = (drag_key is not None and dk[1:] == drag_key[1:3] and _same_element(drag_key[3], sb))
                drag_key, drag_n = dk + (sb,), (drag_n + 1 if same else 1)
                if drag_n >= REPEAT_DRAG_N:
                    f = stuck.ban(tick, "drag", sb, src_desc, drag_n)
                    print(f"           repeat drag: '{short(src_desc, 50)}' -> same target {drag_n}x, state unchanged "
                          f"-> excluded for {args.ban_ticks} ticks")
                    rec["repeat_drag_ban"] = {"src": src_desc, "n": drag_n}
                    drag_key, drag_n = None, 0
            else:
                drag_key, drag_n = None, 0
            recent_inputs.append(executed.split(";")[0] if not veto else "veto")
            if len(recent_inputs) >= REPEAT_WINDOW:
                top_in = max(set(recent_inputs), key=list(recent_inputs).count)
                n_top = list(recent_inputs).count(top_in)
                if n_top >= REPEAT_STOP and top_in not in ("wait",):
                    stop_reason = f"stalled: '{top_in}' {n_top} times in the last {REPEAT_WINDOW} ticks"
            # hard stall stops: same manual step + same TOD pick N ticks running; stamp press refused M times
            pk = (step[0], D["tod_pick"][0], short(desc.get(D["tod_pick"][1], D["tod_pick"][1]), 60))
            pick_key, pick_n = pk, (pick_n + 1 if pk == pick_key else 1)
            if args.pick_stop and pick_n >= args.pick_stop:
                stop_reason = f"stalled: manual step {pk[0]} + TOD pick {pk[1]} '{pk[2]}' {pick_n} ticks running"
            if veto and veto.startswith("refused") and D["tod_pick"][1] == src:
                # counted only when TOD itself picked that stamp (run 005956 t4: a convention re-pick landed on it)
                refused_n += 1
                if args.refuse_stop and refused_n >= args.refuse_stop:
                    stop_reason = f"stalled: stamp press refused {refused_n} times"
            ent.last_under = facts.get("passport_under") or []
            rec["passport_under"] = ent.last_under
            ent.after_action(tick, state, action, sb, tb, frame, changed)
            if len(ent.log) != n_resets:
                n_resets = len(ent.log)
                refused_n, pick_key, pick_n, stall_key, stall_n, drag_key, drag_n = 0, None, 0, None, 0, None, 0

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
            "ticks": len(rows), "gt at end (eval only)": _gt_snapshot(), "TOD calls": tod.total_calls, "cost": f"${tod.total_cost:.4f}",
            "dry_run": args.dry_run, "args": " ".join(sys.argv[1:]),
            "last state": state_line(last_state) if last_state else "-",
            "stop reason": stop_reason or "max ticks",
            "extractor": args.extractor,
            "entrant resets": "; ".join(f"t{t}: {w}" for t, w in ent.log) or "-",
            "remote extract fallbacks": len(getattr(sys.modules.get("tod_papers.extract_remote"), "FALLBACK_LOG", [])),
            "TOD retries": getattr(tod, "total_retries", 0),
        })
        print(f"[loop] done. TOD calls={tod.total_calls} cost=${tod.total_cost:.4f}. Logs: {run_dir}")
    args.result = {"run_dir": run_dir, "stop_reason": stop_reason, "ticks": len(rows)}
    if stop_screens and not (stop_reason or "").startswith("screen"):
        return 3   # asked to stop on a screen that never came
    return 0


def main(argv=None, result: dict | None = None) -> int:
    ap = argparse.ArgumentParser(description="TOD Set-of-Mark agent loop for Papers, Please")
    ap.add_argument("--max-ticks", type=int, default=10)
    ap.add_argument("--extractor", choices=("vision", "static", "hybrid"), default="hybrid",
                    help="vision: extract.extract (models); static: layout.extract_static (fixed layout, no "
                         "models); hybrid: static fixed controls + drop targets, vision documents")
    ap.add_argument("--dry-run", action="store_true", help="do everything except mouse input")
    ap.add_argument("--max-marks", type=int, default=25, help="element cap (soft: text, page corners and "
                    "detector-labelled objects are never dropped; drop-target regions come on top)")
    ap.add_argument("--send-format", choices=("png", "jpeg"), default="png", help="png: smaller than jpeg on pixel art (~80 vs ~170 KB)")
    ap.add_argument("--jpeg-quality", type=int, default=90)
    ap.add_argument("--history", type=int, default=10, help="past actions listed in the request-2 text")
    ap.add_argument("--frames", nargs="+", help="offline: run on saved frames (no game window, no input)")
    ap.add_argument("--out", default=None, help="offline: output directory")
    ap.add_argument("--day", choices=("1", "2", "3"), default=None, help="offline only: day the saved frames are from")
    ap.add_argument("--send-width", type=int, default=1140, help="downscale annotated frame to this width for TOD (0=full)")
    ap.add_argument("--settle", type=float, default=0.12, help="seconds between hover/down/up so Unity sees separate frames")
    ap.add_argument("--post-wait", type=float, default=0.9, help="seconds after input before the final verify grab")
    ap.add_argument("--post-wait-early", type=float, default=0.35, help="early verify grab; a change seen here ends "
                    "the wait")
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
    ap.add_argument("--stop-on-screen", default="", help="comma list of request-1 screens; stop (without acting) "
                    "once one of them is seen --stop-consecutive ticks in a row (used by tools/reset_game.py)")
    ap.add_argument("--stop-consecutive", type=int, default=2)
    ap.add_argument("--fg-patience", type=int, default=60, help="foreground checks (2 s apart) to wait for the game "
                    "window before a safety abort; no input is sent while waiting")
    ap.add_argument("--stall-stop", type=int, default=0, help="stop when screen + state summary stay identical "
                    "for N ticks (0 = off)")
    ap.add_argument("--pick-stop", type=int, default=10, help="stop when the manual step and TOD's pick stay the "
                    "same for N ticks (0 = off)")
    ap.add_argument("--unreachable-stop", type=int, default=3, help="stop after N consecutive ticks whose TOD "
                    "request failed with retries exhausted on a network error (0 = off)")
    ap.add_argument("--refuse-stop", type=int, default=5, help="stop after N refused stamp presses (0 = off)")
    args = ap.parse_args(argv)
    if args.frames:
        return offline(args)
    try:
        rc = run(args)
        if result is not None:
            result.update(getattr(args, "result", {}))
        return rc
    except AbortSafety as e:
        print(f"[loop] SAFETY ABORT: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
