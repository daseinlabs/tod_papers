"""loop.py -- the TOD Set-of-Mark agent loop.

Each tick:  wait-until-stable grab
            -> extract -> REQUEST 1 (state, unmarked frame + desk OCR text): screen, day, the
               booth facts the manual is keyed on (person at window, document on counter,
               open passport, tray open, passport under stamp, stamp ink, passport returned) and the
               readings (issuing country; request 1b: EXP. date, ISS. city, ticket, paper identities)
               plus TOD's VERDICT (approved / denied / cannot_decide_yet) -- all TOD answers
            -> extract -> drop-target regions (stamp landing strip, counter shelf, desk;
               derived from detected boxes, else not offered) -> som
            -> REQUEST 2 (action, SoM frame): the whole manual (manual.py) + what request 1
               says is true + the last 30 actions; questions action / source / target
            -> TOD's action / source / target executed as answered (a click/drag convention
               mismatch is only logged) -> execute -> verify -> log.

TOD makes every decision: what is on screen, the verdict, what kind of input (click/drag/wait),
which numbered element, which drop target. One image per request. The loop adds geometry
(drop-target regions, drop points), bookkeeping (entrant memory of TOD's own answers) and
guards that refuse or exclude (stamp press refused unless TOD put the passport under that
stamp; no-effect / cycle exclusions). Region sources (static/derived/derived_frame/dropped)
are written to every tick json.

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
    if win32gui.GetForegroundWindow() != hwnd:
        # a Windows notification toast (ShellExperienceHost 'New notification', run 101823) keeps the foreground
        # against SetForegroundWindow; a click on the game's TITLE BAR (non-client area, not game input) takes it back
        L, T, _, _ = win32gui.GetWindowRect(hwnd)
        _, cy, _, _ = io_win.client_rect_physical(hwnd)
        if cy - T >= 20:
            x, y = L + 200, T + (cy - T) // 2
            ax, ay = io_win._abs_from_screen(x, y)
            io_win._move_abs_screen(x, y)
            time.sleep(0.05)
            io_win._send_mouse(io_win.MOUSEEVENTF_MOVE | io_win.MOUSEEVENTF_ABSOLUTE | io_win.MOUSEEVENTF_VIRTUALDESK
                               | io_win.MOUSEEVENTF_LEFTDOWN, ax, ay)
            time.sleep(0.05)
            io_win._send_mouse(io_win.MOUSEEVENTF_LEFTUP)
            time.sleep(0.3)
            print(f"[focus] title-bar click to take the foreground back -> {win32gui.GetForegroundWindow() == hwnd}")
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
    why: str = ""            # set for cycle bans: the line shown under RULED OUT instead of 'did nothing'


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

    def ban(self, tick: int, action: str, b: Box, desc: str, count: int, ticks: int | None = None,
            why: str = "") -> "_Fail":
        """Exclude an element now (repeat-drag / cycle rule: it 'changed' pixels but not the state)."""
        f = self._find(action, b)
        if f is None:
            f = _Fail(action, b, desc)
            self.fails.append(f)
        f.box, f.desc, f.last_tick, f.count = b, desc, tick, max(f.count, count)
        f.banned_until = tick + 1 + (self.ban_ticks if ticks is None else ticks)
        f.why = why
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


CYCLE_WINDOW = 8        # ticks looked at for a period-2/3 cycle
CYCLE_BAN_TICKS = 6     # the cycle's sources stay excluded this long
CYCLE_STATE_KEYS = ("person_at_window", "document_on_counter_shelf", "document_open_on_desk", "stamp_tray_open",
                    "passport_shows_stamp_mark", "inspect_mode_on", "bulletin_or_rulebook_covering_desk")


def _desc_key(d: str) -> str:
    """An element description without its '(TOD 0.83)' score and '(middle-left)' position tail (stable per tick)."""
    d = re.sub(r"\s*\(TOD [0-9.]+\)", "", d or "")
    return re.sub(r"\s*\((?:top|middle|bottom|centre)[a-z-]*\)\s*$", "", d).strip()[:80]


@dataclass
class CycleDetector:
    """Generic A,B,A,B / A,B,C,A,B,C detector (run 115900 t118-142: tray open -> close -> open ... 25 ticks; every
    drag changed pixels, so no per-case stop fired). Per tick it keeps a compact state signature (screen, request-1
    yes/no facts, manual step, executed input + source + target) and a progress signature from the loop's own
    entrant bookkeeping (never gt). Two full periods of the same 2- or 3-signature sequence inside the last
    CYCLE_WINDOW ticks with no progress change = a cycle. A guard like the others: it offers nothing new."""
    window: int = CYCLE_WINDOW
    seq: list = field(default_factory=list)   # [(tick, sig, progress, action, src_box, src_desc, tgt_desc)]
    hits: int = 0                             # detections for the current entrant

    def reset(self) -> None:
        self.seq, self.hits = [], 0

    def push(self, tick: int, sig: tuple, progress: tuple, action: str, sb, src_desc: str, tgt_desc: str):
        """Record one executed tick; return {'period', 'n', 'actions': [(action, box, src, tgt)]} on a cycle."""
        if self.seq and self.seq[-1][0] != tick - 1:
            self.seq = []   # only consecutive ticks form a cycle
        self.seq = (self.seq + [(tick, sig, progress, action, sb, src_desc, tgt_desc)])[-self.window:]
        for per in (2, 3):
            n = 2 * per
            if len(self.seq) < n:
                continue
            last = self.seq[-n:]
            sigs = [x[1] for x in last]
            if len(set(sigs[:per])) < 2 or any(sigs[k] != sigs[k + per] for k in range(per)):
                continue
            if len({x[2] for x in last}) != 1 or all(x[3] in ("wait", "veto") for x in last[:per]):
                continue
            self.hits += 1
            acts = [(x[3], x[4], x[5], x[6]) for x in last[:per]]
            self.seq = []   # a second detection needs a fresh pair of periods
            return {"period": per, "n": n, "actions": acts, "hits": self.hits}
        return None


def cycle_signature(screen: str, state: dict, step: str, action: str, src_desc: str, tgt_desc: str) -> tuple:
    facts = tuple(int(man.yes(state, k)) for k in CYCLE_STATE_KEYS)
    return (screen, facts, step, action, _desc_key(src_desc), _desc_key(tgt_desc) if action == "drag" else "")


def cycle_progress(ent: "Entrant") -> tuple:
    """Progress = the loop's own entrant bookkeeping: resets so far (processed count), passport placed under a
    stamp, stamp presses, hand-back, waiting for the rest of the papers."""
    return (len(ent.log), bool(ent.last_under), len(ent.stamp_clicks), ent.handed_back, ent.waiting_docs)


def cycle_callout(hit: dict) -> str:
    parts = [f"{a} '{short(_desc_key(s), 50)}'" + (f" -> '{short(_desc_key(t), 40)}'" if a == "drag" and t else "")
             for a, _, s, t in hit["actions"]]
    alt = " and ".join(parts) if len(parts) == 2 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return (f"The last {hit['n']} actions alternated {alt} with no progress (no passport placed, stamped or handed "
            f"back); neither is the answer. Look at what is on screen again.")


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


def screen_family(frame: np.ndarray) -> tuple:
    """('booth'|other, tray open on the pixels, stamp strips with a paper in them on the pixels or None) from the
    static layout (~8 ms, no model). Only chooses WHICH questions request 1 asks; every answer still comes from
    TOD. Agreed with TOD's booth/non-booth answer in 284/286 ticks of runs 033604/053852/054238."""
    try:
        n = layout.to_native(frame)
        fam, _ = layout.screen_of(n)
        tray = bool(layout.tray_open(n)) if fam == "booth" else False
        return fam, tray, (list(layout.passport_under(n)) if tray else [])
    except Exception:
        return "booth", True, None


REQ1_MAX_Q = 14   # request-1 budget (TOD's hard limit is TOD_MAX_Q; ~80 ms per question)
NONPASSPORT_IDS = ("rulebook", "bulletin", "citation", "flyer", "transcript")


def probe_context(prev_facts: dict | None, prev_state: dict | None) -> dict:
    """What the previous tick established (TOD's answers + the entrant memory), used only to choose WHICH request-1
    questions can matter this tick -- never as an answer. Request 1 is sent before this tick's extraction."""
    pf, ps = prev_facts or {}, prev_state or {}
    dn = pf.get("docs_named") or []
    papers = (man.yes(ps, "document_on_counter_shelf") or man.yes(ps, "document_open_on_desk", 0.3)
              or any(not (d["id"] in NONPASSPORT_IDS and d["p"] >= 0.6) for d in dn))
    pp_desk = man.yes(ps, "document_open_on_desk") or any(
        d["where"] == "desk" and d["id"] == "passport" and d["p"] >= 0.5 for d in dn)
    cover = any(d["where"] == "desk" and d["id"] in ("rulebook", "bulletin") and d["p"] >= 0.3 for d in dn)
    pressed = bool(pf.get("stamp_clicks") or pf.get("missed_stamps"))
    return {"stamped": pressed, "papers": bool(papers), "covering_possible": bool(pp_desk and cover),
            # STAMP_INK_Q (audit B19: 'stamped' is TOD's ink answer): after a press, once ink was read, or while
            # the passport lies under a stamp head (a loop restart loses the press memory)
            "ask_ink": bool(pressed or pf.get("mark_side") or pf.get("passport_under")),
            # PASSPORT_RETURNED_Q (audit B20): after a drop on the person, a press or ink, or once handed back
            "ask_returned": bool(man.yes(ps, "person_at_window") and (
                pressed or pf.get("mark_side") or pf.get("hb_drop") is not None
                or pf.get("handed_back") is not None or pf.get("waiting_docs")))}


def select_state_questions(q: dict, ctx: dict | None, strip_px: list | None) -> list[str]:
    """Booth request 1: drop the questions whose answer nothing would consume this tick (audit section 4).
    Returns the dropped keys (logged). ctx None (first tick, offline frames) keeps everything."""
    drop = []
    # the strip questions are always asked while the tray is open (audit B27: the pixel strip test no longer
    # replaces TOD's answer; it is only a tie-breaker in passport_sides)
    if ctx is not None:
        if not ctx["covering_possible"]:
            drop.append("bulletin_or_rulebook_covering_desk")   # needs a passport AND a rulebook/bulletin on the desk
        if not ctx["papers"]:
            # nothing on the counter or desk last tick: the passport readings are dropped by gate_inspection anyway
            drop += [k for k in (*man.INSPECT_KEYS, "entry_ticket_dated_today")]
    out = [k for k in drop if q.pop(k, None) is not None]
    return out


def state_probe(tod: TodClient, frame: np.ndarray, args, day: str = "unknown", inspect: tuple = man.INSPECT_KEYS,
                facts: dict | None = None, with_docs: bool = True, family: tuple | None = None,
                prev: dict | None = None, ctx: dict | None = None):
    """REQUEST 1: plain (unmarked) frame + short text + screen, day, the manual's state questions
    (manual.state_questions, incl. the passport-under-each-stamp questions) and, with_docs only (offline), one
    identity question per paper the layout found on the desk/counter (its position + the OCR text inside it).
    Only the questions whose answers this tick can consume are asked (select_state_questions; ctx =
    probe_context of the previous tick). Every judgement about the screen is a TOD answer here; the loop only does
    geometry and bookkeeping."""
    today = man.DAY_DATES.get(day, man.DAY_DATES["1"])
    q = {"screen": choice("Which kind of screen is currently shown?", dict(SCREENS)), "day": DAY_Q}
    fam, tray_px, strip_px = (tuple(family) + (None,))[:3] if family else ("booth", True, None)
    if fam != "booth":
        # menus, day-end, bulletins, cutscenes: the manual consumes only the screen kind and the date
        small = _small_for_send(frame, args)
        return tod.ask(q, text=man.STATE_TEXT, image_data_url=encode_image(small, args.send_format, args.jpeg_quality))
    if day in DAY_RULES:
        del q["day"]   # the day only changes on the day-end / bulletin screens, where it is asked
    q.update(man.state_questions(today, inspect, prev))
    q.pop("passport_open_readable", None)   # the inspection gate also opens on open-on-desk / the paper identity
    if not tray_px or (facts or {}).get("tray_open_px") is False:
        for k in man.STRIP_KEYS:   # no stamp bar on the pixels: nothing can lie under a stamp head
            q.pop(k, None)
    dropped = select_state_questions(q, ctx, strip_px if tray_px else None)
    if ctx is None or ctx.get("ask_ink") or (facts or {}).get("ask_ink"):
        q["passport_stamp_ink"] = man.STAMP_INK_Q   # 'stamped' = this answer (manual.stamped), never a pixel diff
    if ctx is None or ctx.get("ask_returned") or (facts or {}).get("ask_returned"):
        q["passport_returned"] = man.PASSPORT_RETURNED_Q   # hand-back / G2 state from the frame
    if facts is not None:
        facts["q_dropped"] = dropped
    docs = (facts or {}).get("docs") or []
    # lowest-value state questions give way to 2 paper identities, then the request-1 budget
    for k in ("bulletin_or_rulebook_covering_desk", "passport_open_readable"):
        if len(q) + (min(2, len(docs)) if with_docs else 0) > REQ1_MAX_Q:
            q.pop(k, None)
    for k in ("bulletin_or_rulebook_covering_desk", "passport_open_readable", "photo_matches_person",
              "inspect_mode_on"):
        if len(q) > TOD_MAX_Q:   # the ink / returned questions may take the request over REQ1_MAX_Q, never TOD's cap
            q.pop(k, None)
            if facts is not None:
                facts.setdefault("q_dropped", []).append(k)
    room = max(0, REQ1_MAX_Q - len(q)) if with_docs else 0
    for i, d in enumerate(docs if with_docs else ()):
        c = doc_cache_get(d, (facts or {}).get("tick", 0))
        if c:
            d["cached"] = c   # same paper, same place, same caption as last tick: reuse TOD's answer
        elif room > 0:
            q[f"doc{i}"] = man.doc_question(d)
            room -= 1
    small = _small_for_send(frame, args)
    # with_docs=False: sent in parallel with extraction, before the desk OCR exists (the image carries the text)
    text = man.STATE_TEXT + ("\n\n" + man.desk_text_block(facts) if with_docs else "")
    return tod.ask(q, text=text, image_data_url=encode_image(small, args.send_format, args.jpeg_quality))


INSP_KEYS_1B = ("exp_date", "ticket_date", "issuing_city_tok", "issuing_city_spelling")
INSP_CACHE: dict = {}   # last fresh 1b passport readings: {"q", "ans", "tick", "seen", "boxes"}
INSP_CACHE_TICKS = 8    # a reading is re-asked at the latest this many ticks after TOD gave it


def _desk_boxes(df: dict) -> list:
    return [d["native"] for d in df.get("docs") or [] if d["where"] == "desk"]


def _same_boxes(a: list, b: list) -> bool:
    return len(a) == len(b) and all(any(_iou_t(x, y) > DOC_CACHE_IOU for y in b) for x in a)


def doc_probe(tod: TodClient, frame: np.ndarray, args, facts: dict, day: str = "1", docs: bool = True):
    """REQUEST 1b (after extraction): one identity question per paper box not in DOC_CACHE (unmarked frame + the
    OCR text inside each) and, on Day 2/3, the passport readings as choices over the OCR'd strings on the desk
    (EXP. date among the OCR dates, ISS. city among the OCR city words, spelling contrast; manual.
    inspection_doc_questions; the candidates go to facts['insp_cand']). The readings are not re-asked while the
    same questions (same OCR candidates) stand over the same, unmoved desk papers as last tick: TOD's previous
    answers go to facts['insp_reuse'] (INSP_CACHE, at most INSP_CACHE_TICKS old). Returns the TOD result or None."""
    q = {}
    tick = facts.get("tick", 0)
    if day in ("2", "3"):
        iq, facts["insp_cand"] = man.inspection_doc_questions(facts.get("desk_text") or [], day)
        facts["insp_q"] = iq
        c = INSP_CACHE
        if (iq and c.get("q") == iq and c.get("seen", -9) >= tick - 1 and tick - c["tick"] <= INSP_CACHE_TICKS
                and _same_boxes(_desk_boxes(facts), c.get("boxes") or [])):
            facts["insp_reuse"] = {k: dict(v, reused_from=c["tick"]) for k, v in c["ans"].items()}
            c["seen"] = tick
        else:
            q.update(iq)
    if facts.get("ask_verdict"):
        # audit A1: the verdict is TOD's answer -- today's rule + TOD's own earlier readings, no code comparison
        q["verdict"] = man.verdict_question(day, facts.get("verdict_mem") or {})
    for i, d in enumerate((facts.get("docs") or []) if docs else []):
        c = doc_cache_get(d, tick)
        if c:
            d["cached"] = c
        elif len(q) < TOD_MAX_Q:
            q[f"doc{i}"] = man.doc_question(d)
    if not q:
        return None
    small = _small_for_send(frame, args)
    # with the Day 2/3 readings in the request the OCR block stays out of the text: listed there, the OCR's misread
    # spelling ('Paradizng') pulled the spelling contrast to S1 on 2 valid passports (dry run loop8_d2dry); the
    # identity questions carry the OCR text of their own paper anyway
    text = (man.STATE_TEXT if any(k in q for k in ("exp_date", "issuing_city_tok"))
            else man.STATE_TEXT + "\n\n" + man.desk_text_block(facts))
    return tod.ask(q, text=text,
                   image_data_url=encode_image(small, args.send_format, args.jpeg_quality))


def merge_doc_answers(state: dict, df: dict, tick: int) -> None:
    """After request 1b: reused passport readings into the state (fresh answers win), fresh ones into INSP_CACHE,
    then the readings -> exp_read / issuing_city / ticket (manual.read_inspection_answers)."""
    for k, v in (df.get("insp_reuse") or {}).items():
        state.setdefault(k, v)
    fresh = {k: state[k] for k in INSP_KEYS_1B if k in state and "reused_from" not in state[k]}
    if fresh and df.get("insp_q"):
        INSP_CACHE.clear()
        INSP_CACHE.update(q=df["insp_q"], ans=fresh, tick=tick, seen=tick, boxes=_desk_boxes(df))
    if df.get("insp_cand"):
        man.read_inspection_answers(state, df["insp_cand"])


def add_tod_facts(facts: dict, state: dict, df: dict, sinfo: dict | None) -> None:
    """facts += TOD's document identities, what lies under each stamp head, and the passport_under sides."""
    facts["static"] = sinfo
    facts["docs_named"] = df["docs_named"] = name_docs(state, df)
    for i, d in enumerate(df.get("docs") or []):
        a = state.get(f"doc{i}")
        if a:
            doc_cache_put(d, a, facts.get("tick", 0))
    derive_open_on_desk(state, facts)
    facts["strip"] = strip_facts(state, df, sinfo)
    facts["passport_under"] = passport_sides(facts["strip"])
    facts["tray_open_px"] = bool((sinfo or {}).get("tray_open"))
    facts["clutter"] = clutter_facts(facts["docs_named"], facts["tray_open_px"])
    if not man.yes(state, "person_at_window") and not man.yes(state, "document_open_on_desk"):
        # loop14: the entrant leaves as soon as the passport is back; an entry ticket still on the desk then is
        # left behind (TOD: nobody at the window) -> desk clutter, manual step K (stow), never a hand-back
        for d in facts["docs_named"]:
            if d["id"] == "entry_ticket" and d["p"] >= CLUTTER_P and d["where"] == "desk" and d.get("native"):
                facts["clutter"].append({"id": "entry_ticket", "p": d["p"], "native": d["native"], "where": "desk",
                                         "strips": [], "under_bar": False, "on_passport": False, "in_way": True,
                                         "left_behind": True})


CLUTTER_IDS = ("citation", "flyer")   # papers that are never stamped / checked (manual step K)
CLUTTER_P = 0.5


def clutter_facts(named: list[dict], tray_open: bool) -> list[dict]:
    """Where each citation slip / flyer TOD named (request-1 identity, p >= CLUTTER_P) lies, from layout geometry
    only: on a stamp landing strip, under the open tray bar (above the strip, hidden by the bar), on the passport, or
    elsewhere. in_way = it occupies the stamp area or the passport (loop10 run 164732 t92-121: a flyer and citation
    slips under the tray / on the DENIED strip, 32-tick stall; run 161058 Uvilia: 71 ticks)."""
    pps = [d["native"] for d in named if d["id"] == "passport" and d["p"] >= 0.5 and d["where"] == "desk"]
    bx1, by1, bx2, _ = layout.TRAY_BAR
    sy1, sy2 = layout.STRIP_Y
    out = []
    for d in named:
        if d["id"] not in CLUTTER_IDS or d["p"] < CLUTTER_P or not d.get("native"):
            continue
        a, b, c, e = d["native"]
        area = max(1, (c - a) * (e - b))
        if d["where"] != "desk":
            out.append({"id": d["id"], "p": d["p"], "native": d["native"], "where": d["where"], "strips": [],
                        "under_bar": False, "on_passport": False, "in_way": False})
            continue
        strips = [sd for sd, (x1, x2) in layout.STRIP_X.items()
                  if min(c, x2) - max(a, x1) >= 0.4 * (x2 - x1) and b <= sy2 and e >= sy1]
        bar = max(0, min(c, bx2) - max(a, bx1)) * max(0, min(e, sy1) - max(b, by1)) / area
        on_pp = any(p != d["native"] and _iou_t(p, d["native"]) >= 0.1 for p in pps)
        under = bool(tray_open and bar >= 0.3)
        out.append({"id": d["id"], "p": d["p"], "native": d["native"], "where": "desk", "strips": strips,
                    "under_bar": under, "on_passport": on_pp,
                    "in_way": bool(strips or on_pp or bar >= 0.3)})
    return out


# an unchanged paper is not re-asked: same place (counter/desk), same printed-text caption (extract.TEXT_CAPS on its
# OCR lines), box IoU > DOC_CACHE_IOU with the box seen last tick. Under the old exact-text key OCR jitter on an
# unmoved paper re-asked it most ticks (audit section 4: ~3 doc questions per request)
DOC_CACHE: list = []   # [{"where", "cap", "box", "id", "p", "tick" (asked), "seen" (last matched)}]
DOC_CACHE_TICKS = 15    # an identity is re-asked at the latest this many ticks after TOD gave it
DOC_CACHE_IOU = 0.9
DOC_CACHE_MIN_P = 0.5   # an identity below this p is reused for DOC_CACHE_UNSURE_TICKS only
DOC_CACHE_UNSURE_TICKS = 3


def _iou_t(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def _doc_cap(d: dict) -> str:
    t = " ".join(d.get("text") or ())
    return next((cap for rx, cap in ex.TEXT_CAPS if rx.search(t)), "")


def doc_cache_get(d: dict, tick: int) -> dict | None:
    cap = _doc_cap(d)
    for c in DOC_CACHE:
        if (c["where"] == d["where"] and c["cap"] == cap and tick - 1 <= c["seen"] <= tick
                and tick - c["tick"] <= (DOC_CACHE_TICKS if c["p"] >= DOC_CACHE_MIN_P else DOC_CACHE_UNSURE_TICKS)
                and _iou_t(c["box"], d["native"]) > DOC_CACHE_IOU):
            c["seen"], c["box"] = tick, list(d["native"])
            return {"id": c["id"], "p": c["p"], "tick": c["tick"]}
    return None


def doc_cache_put(d: dict, a: dict, tick: int) -> None:
    DOC_CACHE[:] = [c for c in DOC_CACHE if tick - c["seen"] <= 1
                    and not (c["where"] == d["where"] and _iou_t(c["box"], d["native"]) > DOC_CACHE_IOU)]
    DOC_CACHE.append({"where": d["where"], "cap": _doc_cap(d), "box": list(d["native"]), "id": a["value"],
                      "p": a["p"], "tick": tick, "seen": tick})


def name_docs(state: dict, df: dict) -> list[dict]:
    """df['docs'] + TOD's identity answer for each (state['doc<i>'], or the cached answer for an unchanged paper)."""
    out = []
    for i, d in enumerate(df.get("docs") or []):
        a = state.get(f"doc{i}")
        a = a if a else ({"value": d["cached"]["id"], "p": d["cached"]["p"]} if d.get("cached") else None)
        if not a:
            continue
        if a["p"] < man.IDENTITY_MIN_P:
            # identity gate (ticket_flyer_identity notes section 6): a near-uniform answer is no identity. The
            # paper is 'unread' and never counts as a ticket, flyer or passport for any step
            out.append({**d, "id": man.UNREAD, "raw_id": a["value"], "p": a["p"]})
        else:
            out.append({**d, "id": a["value"], "p": a["p"]})
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
               "entry_ticket_dated_today": "ticket", "no_documents_presented": "nodocs",
               "interrogate_prompt_visible": "interr"}


def state_line(state: dict) -> str:
    """Compact all-answers line (p = P(yes)) for the console and the overlay banner."""
    bits = []
    if "screen" in state:
        bits.append(f"{state['screen']['value']}:{state['screen']['p']:.2f}")
    for k, ab in _STATE_ABBR.items():
        if k in state:
            bits.append(f"{ab}={state[k]['p']:.2f}")
    if "rulebook_page" in state:
        bits.append(f"rb={state['rulebook_page']['value']}:{state['rulebook_page']['p']:.2f}")
    if "issuing_country" in state:
        bits.append(f"iss={state['issuing_country']['value']}:{state['issuing_country']['p']:.2f}")
    for k, ab in (("exp_read", "exp"), ("issuing_city_spelling", "spell")):
        if k in state:
            bits.append(f"{ab}={state[k]['value']}:{state[k]['p']:.2f}")
    if "issuing_city" in state:
        bits.append(f"city={state['issuing_city']['value']}:{state['issuing_city']['p']:.2f}")
    for k, ab in (("passport_stamp_ink", "ink"), ("passport_returned", "ret"), ("verdict", "VERDICT")):
        if k in state:
            bits.append(f"{ab}={state[k]['value']}:{state[k]['p']:.2f}")
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


STRIP_TOD_P = 0.6   # TOD's strip answer decides at p >= this (yes) or <= 1 - this (no); in between the pixel
# paper test + TOD's identity of the strip paper break the tie (logged as strip[side]['source'] = 'pixel_tiebreak')


def passport_sides(strip: dict) -> list[str]:
    """Stamp heads with the PASSPORT under them = TOD's passport_under_<side> answers (audit B27/D10). A paper TOD's
    identity names as something else (entry ticket, rulebook ... at p >= 0.6; a citation / flyer) is not the passport
    unless the strip answer is confident (run 075504 t112-114; 161058 t95-96). Only when TOD is unsure both ways
    (p between 0.4 and 0.6, or not asked) does the pixel 'paper in the strip' test, together with TOD's identity
    'passport' of the paper over that strip, break the tie; each side records strip[side]['source']."""
    out = []
    for s_, v in strip.items():
        pp = v["passport_p"]
        if v["doc"] not in (None, "passport", man.UNREAD) and (v["doc_p"] or 0) >= 0.6 and (pp or 0) < 0.75:
            v["source"] = "tod_identity_not_passport"
            continue
        if v["doc"] in CLUTTER_IDS and (v["doc_p"] or 0) >= CLUTTER_P and (pp or 0) < 0.85:
            v["source"] = "tod_identity_clutter"
            continue
        if pp is not None and pp >= STRIP_TOD_P:
            v["source"] = "tod"
            out.append(s_)
        elif pp is not None and pp <= 1 - STRIP_TOD_P:
            v["source"] = "tod"
        else:
            tie = bool(v["paper"] and v["doc"] == "passport" and (v["doc_p"] or 0) >= 0.5)
            v["source"] = "pixel_tiebreak"
            if tie:
                out.append(s_)
    return out


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


DESK_TARGET_HALF = 12    # native px: the desk target is a small box centred on the computed drop point
CLEAR_SPOT_IOU = 0.8     # the open passport already lies on the clear spot -> 'clear desk space' is not offered


def _passport_doc(facts: dict | None) -> dict | None:
    """The paper TOD named the passport (p >= 0.5): the largest one on the desk, else one on the counter."""
    pp = [d for d in (facts or {}).get("docs_named") or [] if d.get("id") == "passport" and d.get("p", 0) >= 0.5
          and d.get("native")]
    area = lambda d: (d["native"][2] - d["native"][0]) * (d["native"][3] - d["native"][1])
    desk = sorted((d for d in pp if d["where"] == "desk"), key=lambda d: -area(d))
    return desk[0] if desk else next((d for d in pp if d["where"] == "counter"), None)


def desk_target(frame: np.ndarray, facts: dict | None, state: dict):
    """Desk drop target derived from this frame (runs 092642/115900/150111: the fixed desk box sent every
    counter->desk passport to (268,267) -> open box y 186..348, the bottom 28 px with EXP./country off the frame,
    and with the tray open its top-right under the stamp bar; 14/23 next reads failed). layout.clear_desk_spot
    places the OPEN passport (size from its visible box, else the fixture) fully on the desk, off the stamp bar /
    knobs / tray tab / inspect button / every other paper, data page first; layout.passport_drop_point turns that
    into the cursor end point (counter: opens centred on the cursor; desk: keeps the grab offset).
    Returns (Box in frame px, info dict) or None without static-layout docs (vision extractor)."""
    sinfo = (facts or {}).get("static") or {}
    if "docs" not in sinfo:
        return None
    H, W = frame.shape[:2]
    tray = bool(sinfo.get("tray_open", man.yes(state, "stamp_tray_open")))
    src = _passport_doc(facts)
    open_src = src if (src and src["where"] == "desk"
                       and src["native"][2] - src["native"][0] >= 0.75 * layout.OPEN_PASSPORT[0]) else None
    size = layout.open_passport_size(open_src["native"] if open_src else None)
    exclude = [open_src["native"]] if open_src else []
    spot = layout.clear_desk_spot(sinfo.get("docs") or [], tray, size, bool(sinfo.get("inspect_button")), exclude)
    nx, ny = layout.passport_drop_point(spot, open_src, size, tray)
    sx, sy = W / layout.NATIVE_W, H / layout.NATIVE_H
    hw = DESK_TARGET_HALF
    box = Box(int((nx - hw) * sx), int((ny - hw) * sy), int((nx + hw) * sx), int((ny + hw) * sy), "", "region", 0.0,
              caption=REGION_CAPS["desk"])
    cur = layout.full_passport_box(open_src["native"], size, tray) if open_src else None
    info = {"drop_native": [nx, ny], "passport_box_planned": spot["box"], "check": spot["check"],
            "passport_from": (src or {}).get("where"), "passport_box_now": cur,
            "at_spot": bool(cur is not None and layout._iou4(cur, spot["box"]) >= CLEAR_SPOT_IOU)}
    return box, info


def strip_drop_point(sb: Box, tb: Box, facts: dict | None, frame: np.ndarray, state: dict):
    """Cursor end point (frame px) for a passport dragged onto a stamp landing strip: the VISA page (upper half of
    the open passport) must lie under the stamp head, so the grab offset to the visa-page centre is kept (loop10
    runs 161058 t94-95 / 163640: the grab point -- often on the data page -- was dropped on the strip, the visa page
    ended under the stamp bar and both stamps inked the DATA page: no decision recorded, EXP. covered). A closed
    passport from the counter opens centred on the cursor. Returns None when the source is not the passport."""
    if tb.caption not in (REGION_CAPS["stamp_landing_denied"], REGION_CAPS["stamp_landing_approved"]):
        return None
    src = _passport_doc(facts)
    if src is None:
        return None
    H, W = frame.shape[:2]
    sx, sy = W / layout.NATIVE_W, H / layout.NATIVE_H
    gx, gy = sb.center[0] / sx, sb.center[1] / sy
    vis = list(src["native"])
    if not (vis[0] - 4 <= gx <= vis[2] + 4 and vis[1] - 4 <= gy <= vis[3] + 4):
        return None
    tray = bool(((facts or {}).get("static") or {}).get("tray_open", man.yes(state, "stamp_tray_open")))
    size = layout.open_passport_size(vis if src["where"] == "desk" else None)
    w0 = layout.OPEN_PASSPORT[0]
    if src["where"] == "desk" and 0.8 * w0 <= vis[2] - vis[0] <= 1.2 * w0:   # one clean open passport box
        full = layout.full_passport_box(vis, size, tray)
        vcx, vcy = (full[0] + full[2]) / 2, full[1] + (full[3] - full[1]) * layout.PASSPORT_DATA_FRAC / 2
        ox, oy = gx - vcx, gy - vcy
    elif src["where"] == "counter":
        ox, oy = 0.0, size[1] / 4   # opens centred on the cursor: the visa centre is a quarter height above it
    else:
        return None
    tx, ty = tb.center[0] / sx + ox, tb.center[1] / sy + oy
    m = 3 * layout.DESK_MARGIN   # the cursor must end on the desk
    tx = min(max(tx, layout.DESK[0] + m), layout.DESK[2] - m)
    ty = min(max(ty, layout.DESK[1] + m), layout.DESK[3] - m)
    return int(tx * sx), int(ty * sy)


def passport_needs_clear_space(state: dict, facts: dict | None, day: str, info: dict) -> bool:
    """'clear desk space' target: an open passport lies on the desk (not under a stamp head, not already on the
    clear spot) and request 1b could not read it -- country 'unreadable' / p < 0.6 (none carried), or on Day 2/3
    no EXP. date read (none carried). Only when 1b asked (issuing_country in the state)."""
    if "issuing_country" not in state or info.get("passport_box_now") is None or info.get("at_spot"):
        return False
    if (facts or {}).get("passport_under"):
        return False
    c = state.get("issuing_country") or {}
    country_ok = bool((c.get("value") not in (None, "unreadable") and c.get("p", 0) >= CARRY_COUNTRY_P)
                      or (facts or {}).get("country_carried"))
    exp_ok = (day not in ("2", "3") or bool((state.get("exp_read") or {}).get("value"))
              or bool((facts or {}).get("exp_carried")))
    return not (country_ok and exp_ok)


# --------------------------------------------------------------------------
# drop-target regions (stamp landing strip, counter shelf, desk)
# --------------------------------------------------------------------------

REGION_CAPS = {
    "stamp_landing_approved": "stamp landing strip (under the APPROVED stamp head)",
    "stamp_landing_denied": "stamp landing strip (under the DENIED stamp head)",
    "counter_shelf": "counter shelf under the window",
    "hand_back": "the entrant at the booth window -- drop documents ON THE PERSON to hand them back",
    "desk": "desk (drop documents here to read them)",
    "desk_clear": "clear desk space (move the passport so its page is fully visible)",
    "tray_stow": "right edge of the desk (drag the tray tab here to put the stamp tray away)",
    "stow_papers": "counter shelf left of the desk -- drop the rulebook, bulletin, a flyer or a citation slip here to put "
                   "it away (off the desk)",
}


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
    Anything that cannot be derived (or fails a sanity check) is NOT offered
    (no fixed fallback coordinates; anchors.json was removed): target_source=dropped.
    Returns (regions, {name: 'derived'|'dropped'})."""
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
                regions.append(r)
            else:
                src[name] = "dropped"
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
            regions.append(r)
        else:
            src["tray_stow"] = "dropped"

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
    src["counter_shelf"] = "derived" if shelf is not None else "dropped"   # used only to place the desk
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
        regions.append(hb)
    else:
        src["hand_back"] = "dropped"

    # --- desk: right of the shelf, below the tray
    if src["counter_shelf"] == "derived":
        x1 = shelf.x2 + int(0.03 * W)
        desk = Box(x1, int(0.72 * H), min(W - 1, x1 + int(0.25 * W)), int(0.95 * H), "", "region", 0.0,
                   caption=REGION_CAPS["desk"])
        src["desk"] = "derived"
        regions.append(desk)
    else:
        src["desk"] = "dropped"
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
    "Convention (manual section 2): stamps and buttons are clicked, papers and the tray tab are dragged -- the "
    "loudspeaker/horn, the APPROVED and DENIED stamps, buttons/menu text and page corners are CLICKED; documents "
    "(passport, papers, bulletin, rulebook), the stamp tray tab and the shutter lever are DRAGGED. Drop targets "
    "(stamp landing strip, the entrant, desk) are only the end point of a drag."
)


WAIT_KEY = "wait"
WAIT_DESC = "wait - do nothing this turn (someone is walking in / a screen is changing)"


ACTION_CHOICES = {
    "click": "click one numbered element (stamps, the loudspeaker, buttons / menu text, page corners)",
    "drag": "drag one numbered element (a paper, the stamp tray tab, the lever) onto a numbered drop target",
    "wait": "do nothing this turn (someone is walking in / a screen is changing)",
}


def build_questions(src_ids: dict, tgt_ids: dict, booth: bool = True) -> dict:
    """Request 2: the input kind (TOD's `action`, audit B26), the element and, in the booth, the drop target."""
    q = {
        "action": choice("Following the manual and what is currently true on screen, what kind of mouse input is "
                         "the next step? " + ACTION_RULE, dict(ACTION_CHOICES)),
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
            "document; clear desk space = move a half-hidden open passport so its page shows; tray stow edge / desk "
            "= close / open the stamp tray.) Ignored for a click.", tgt_ids)
    return q


def _cls(b, booth: bool, doc: bool = False):
    if b is None:
        return None
    if b.kind == "background":
        return "click"
    if doc:
        return "drag"   # TOD's request-1 identity says this element is a paper (documents are drag-only)
    return man.input_class(b, booth)


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


CITATION_RE = re.compile(r"CITATION|WARNING|PENALTY|ISSUED|Protocol|Violat|M\.?[O0]\.?[AR]\.", re.I)
LAST_DAY_FILE = os.path.join(ROOT, "runs", "LAST_DAY.json")   # the day TOD last read, for a restart within the hour
PAUSE_MIN_FRAC = 0.05     # changed fraction (vs the grabbed frame) that counts as "pause menu on screen"


MULTIPAGE_IDS = ("rulebook", "bulletin", "transcript")   # papers whose page corner turns a page (a click)
REQ2_MAX_OPTS = 12   # request-2 options (source + target); request 2 costs ~0.1 s per option (audit section 3)
NO_DESK_DRAG = ("tray_tab_open", "shutter_lever")   # drag sources whose drop is never the desk


def paper_groups(named: list) -> list[int]:
    """docs_named index -> index of the first paper it is the same physical paper as: same TOD identity and the
    boxes overlap (IoU >= 0.3, or one box's centre inside the other) -- e.g. a passport split into visa page +
    data page by the layout, both named PASSPORT."""
    grp = list(range(len(named)))
    for j, d in enumerate(named):
        for i in range(j):
            e = named[i]
            if e["id"] != d["id"] or e["where"] != d["where"] or grp[i] != i:
                continue
            a, b = e["box"], d["box"]
            inside = lambda p, q: q[0] <= (p[0] + p[2]) / 2 <= q[2] and q[1] <= (p[1] + p[3]) / 2 <= q[3]
            if _iou_t(a, b) >= 0.3 or inside(a, b) or inside(b, a):
                grp[j] = i
                break
    return grp


def desk_target_redundant(idmap: dict, src_ids: dict, doc_ids, frame, state: dict, day: str,
                          facts: dict | None) -> str | None:
    """Id of the plain 'desk' drop target when no offered drag source could use it this tick: every paper that can
    be dragged already lies on the desk (not under a stamp head), the tray tab offered is the open one (it goes to
    the stow edge), and no other drag source (counter paper, paper slots below the counter, closed tray tab) is
    offered. Coordinator count: 26 passport re-drops onto the desk it already lay on (runs 015649/022439). Kept in
    step N, step E? and whenever the passport lies under a stamp head (it may have to go back to the desk)."""
    f = facts or {}
    desk = [k for k, b in idmap.items() if b.kind == "region" and b.caption == REGION_CAPS["desk"]]
    if not desk or f.get("no_passport") or f.get("passport_under"):
        return None
    # passport not read yet (country, and on Day 2/3 the EXP. date): moving it on the desk re-exposes the page
    # (runs 092642 t10/t105/t116, 150111 t20: desk -> desk moves while the country read 'unreadable')
    if man.known_country(state, f) is None or (day in ("2", "3") and man.known_exp(state, f) is None):
        return None
    dn = f.get("docs_named") or []
    for k in src_ids:
        b = idmap[int(k)]
        if b.kind == "background" or _cls(b, True, int(k) in doc_ids) != "drag":
            continue
        if getattr(b, "name", "") in NO_DESK_DRAG:
            continue
        on = [d for d in dn if d["box"][0] <= b.center[0] <= d["box"][2] and d["box"][1] <= b.center[1] <= d["box"][3]]
        if not (int(k) in doc_ids and on and all(d["where"] == "desk" for d in on)):
            return None   # a counter paper, a slot, the closed tray tab, an unnamed paper: the desk is a real target
        for d in on:
            a, _, c, e = d["native"]
            for x1, x2 in layout.STRIP_X.values():
                if min(c, x2) - max(a, x1) >= 0.4 * (x2 - x1) and d["native"][1] <= layout.STRIP_Y[1] \
                        and e >= layout.STRIP_Y[0]:
                    return None   # the paper lies on a stamp strip: the desk is where it is moved off it
    return str(desk[0])


def cap_options(src_ids: dict, tgt_ids: dict, idmap: dict, doc_ids, booth: bool) -> dict:
    """Booth request-2 sources without detector filler: unnamed icon/panel/text boxes on no paper TOD named that
    carry no caption and no text, or only a 'possibly ...' CLIP guess, are not offered; then down to REQ2_MAX_OPTS
    (sources + targets) by dropping the remaining unnamed detector boxes, largest last. Named layout controls, papers, stamps and every target stay. (Runs 092642/150111: 74 'possibly ...' icon
    and 19 uncaptioned panel options, never picked.)"""
    if not booth:
        return src_ids
    over = len(src_ids) + len(tgt_ids) - REQ2_MAX_OPTS

    def rank(k):
        b = idmap[int(k)]
        if getattr(b, "name", "") or int(k) in doc_ids or b.kind not in ("icon", "panel", "text"):
            return None
        cap = b.caption or ""
        return (0 if not cap and not b.text else 1 if cap.startswith("possibly") else 2, b.area)

    filler = sorted((k for k in src_ids if rank(k) is not None), key=rank)
    # content-free boxes (no caption, no text) and CLIP guesses on no paper go on the booth anyway; the rest by the cap
    drop = {k for k in filler if rank(k)[0] <= 1} | set(filler[:max(0, over)])
    return {k: v for k, v in src_ids.items() if k not in drop}


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
    desk_info = None
    dt = desk_target(frame, facts, state) if booth else None
    if dt is not None:
        # the desk target from this frame's papers + layout fixtures (desk_target), replacing the fixed/derived box
        dbox, desk_info = dt
        name = "desk"
        if passport_needs_clear_space(state, facts, day, desk_info):
            name = "desk_clear"   # same point; offered instead of the plain desk while 1b cannot read the passport
            dbox.caption = REGION_CAPS["desk_clear"]
        regions = [r for r in regions if r.caption != REGION_CAPS["desk"]] + [dbox]
        region_src.pop("desk", None)
        region_src[name] = "derived_frame"
        desk_info["target"] = name
        if facts is not None:
            facts["desk_target"] = desk_info
    if booth and facts is not None and facts.get("waiting_docs"):
        # G2: the remaining papers go to the entrant; the stow shelf (also 'counter shelf ...') took the ticket twice
        # in run 092521 t217-218 while the entrant waited for it
        # only the entrant (and the tray stow edge, to uncover papers under the tray) are drop targets in G2
        keep = (REGION_CAPS["hand_back"], REGION_CAPS["tray_stow"]) + (
            (REGION_CAPS["stow_papers"],) if any(c["in_way"] and c["id"] == "citation"
                                                 for c in facts.get("clutter") or []) else ())
        regions = [r for r in regions if r.caption in keep]   # a citation in the way is never handed to the entrant
    # step N (no passport presented): the entrant stays a drop target (hand back what they gave), the transcript
    # printer stays offered, the tray tab and the stamps are not (run 115900 t118-142: tray open/close 25 ticks)
    nopp = bool(booth and facts is not None and man.no_passport(state, facts))
    if facts is not None:
        facts["no_passport"] = nopp
    if nopp:
        # step N reads the rulebook on the DESK; the stow shelf puts it away (Jorji dry-run: TOD dropped the rulebook
        # on 'counter shelf left of the desk' instead of the desk). The desk stays a drop target.
        regions = [r for r in regions if r.caption != REGION_CAPS["stow_papers"]]
    only_tickets = bool((facts or {}).get("waiting_docs"))
    # only entry tickets left (passport already gone back, e.g. after a loop restart lost the hand-back memory):
    # the entrant target stays offered (run 092521 t226)
    if booth and facts is not None and not man.stamped(state, facts) and not facts.get("waiting_docs") and not nopp:
        # an unstamped passport is not handed back (run 054238 t62-137: 20+ unstamped hand-backs on a false mark
        # reading); the entrant target is offered once a stamp press is on record for this entrant
        regions = [r for r in regions if r.caption != REGION_CAPS["hand_back"]]
    if booth and man.yes(state, "person_at_window", HORN_HIDE_P):
        # the horn only calls someone when the window is empty (runs 114927 t38-97: 50 horn clicks with the
        # entrant standing at the window); TOD says someone is there, so it is not offered
        # the paper stores below the counter (bulletin, rulebook slot, transcript printer) are not needed on Days 1-3
        # and only add papers to the desk (run 044332 t48-52: a fresh bulletin dragged onto the stamp strips;
        # run 005956 t18-31: 14 bulletin-storage drags)
        hide_n = ("horn", "bulletin") + (() if nopp else ("rulebook", "transcript"))   # step N needs the rulebook
        nb = [b for b in boxes if getattr(b, "name", "") not in hide_n and b.caption != "speaker/horn"]
        if len(nb) < len(boxes) and facts is not None:
            facts["horn_hidden"] = True
        boxes = nb
    if booth and not man.yes(state, "inspect_mode_on") and not nopp:   # step N: inspect mode to interrogate
        # run 021438 t3: TOD clicked the red inspect button; inspect mode froze every drag for 8 ticks. Days 1-3 need
        # no inspect mode, so the button is offered only while TOD says inspect mode is on (to leave it)
        boxes = [b for b in boxes if getattr(b, "name", "") != "inspect_toggle"]
    if booth:
        # a press there toggles inspect mode whatever the element is called: run 081222/082950 dragged an entry
        # ticket by its proposed page corner / 'document under the tray' box at (2256,1256)/(2218,1232) -> inspect
        # mode on every drag (H/B ping-pong 20 ticks). Only the inspect button itself may sit on the button.
        ix1, iy1, ix2, iy2 = layout.scale_box(layout.BY_NAME["inspect_toggle"].box, W, H)
        boxes = [b for b in boxes if getattr(b, "name", "") == "inspect_toggle"
                 or not (ix1 <= b.center[0] <= ix2 and iy1 <= b.center[1] <= iy2)]
    # step N's empty-counter click target is layout.py's `counter_empty` element (pixel test: nothing on the shelf)
    if booth:
        # M.O.A. citation slips pile up on the desk after mistakes and are never needed on Days 1-3 (run 104848
        # t329-342: 14 ticks of citation shuffling with the next passport waiting on the counter): not offered
        # loop10: a slip in the working area (strip, under the tray bar, on the passport: clutter_facts) stays offered,
        # drag-only, so step K can move it to the stow shelf (164732 t107-121: E0 named it, nothing could move it)
        way = {tuple(c["native"]) for c in (facts or {}).get("clutter") or [] if c["in_way"]}
        cits = [d["box"] for d in (facts or {}).get("docs_named") or [] if d["id"] == "citation" and d["p"] >= 0.5
                and tuple(d["native"]) not in way]
        keep_b = [d["box"] for d in (facts or {}).get("docs_named") or [] if tuple(d.get("native") or ()) in way]
        others = [d["box"] for d in (facts or {}).get("docs_named") or [] if d["id"] not in ("citation", "other")]
        if cits:   # the slip and everything on it, unless it also lies on a paper TOD named as something else
            def _inside(b, bxs) -> bool:
                return any(x1 <= b.center[0] <= x2 and y1 <= b.center[1] <= y2 for x1, y1, x2, y2 in bxs)

            def _cit(b) -> bool:
                if getattr(b, "name", "") in layout.BY_NAME or _inside(b, keep_b):
                    return False
                if b.text and CITATION_RE.search(b.text):
                    return True
                return _inside(b, cits) and not _inside(b, others)
            boxes = [b for b in boxes if not _cit(b)]
    handle = derive_tray_handle(boxes, frame, state) if booth else None
    if handle is not None:
        boxes = list(boxes) + [handle]
    if (facts or {}).get("waiting_docs"):
        # G2 (TOD said the passport was returned and the entrant is still there): opening the tray is not offered
        # (run 072926 t125-167: 40 ticks of tray open/close with the entry ticket in view); closing it stays
        boxes = [b for b in boxes if getattr(b, "name", "") != "tray_tab"
                 and not (b.caption or "").startswith("tab at screen edge")]
    if nopp:
        boxes = [b for b in boxes if not (_is_tray_tab(b) or b.caption == TRAY_HANDLE_CAP
                                          or (b.caption or "").startswith("tab at screen edge"))]
    # audit A1: both stamps are offered whenever the tray is open -- no stamp is hidden by a code verdict, a press
    # memory or an ink reading. The only stamp guard is the refusal veto in run() (TOD's strip answer).
    tray_note = None
    flips = (facts or {}).get("tray_flips", 0)
    if stuck is not None and flips >= TRAY_FLIP_LIMIT:
        # audit A5: a tray open/close loop is excluded BEFORE request 2 (struck through, reason in the history
        # callout); TOD's pick is never swapped afterwards. Only the CLOSING tab: opening a closed tray is step C.
        for b in boxes:
            if getattr(b, "name", "") == "tray_tab_open" or b.caption == TRAY_HANDLE_CAP:
                why = (f"was opened and closed {flips} times in the last 8 ticks without a stamp (tray toggle "
                       "loop)")
                stuck.ban(tick, "drag", b, describe(b, W, H), 1, ticks=TRAY_BAN_TICKS, why=why)
                tray_note = (f"TRAY LOOP: the stamp tray was opened and closed {flips} times in the last 8 ticks "
                             f"without a stamp; its closing tab is ruled out for {TRAY_BAN_TICKS} ticks.")
    if facts is not None:
        facts["tray_note"] = tray_note
    banned = (lambda b: stuck.banned(tick, b) is not None) if stuck is not None else (lambda b: False)
    annotated, idmap = annotate(frame, list(boxes) + regions, max_marks=args.max_marks - 1 + len(regions),
                                excluded=banned)
    if not booth:  # text/cutscene screens without a button are advanced by clicking the screen itself
        idmap[len(idmap) + 1] = Box(W // 4, H // 4, 3 * W // 4, 3 * H // 4, "", "background", 0.0)
    desc = {str(i): describe(b, W, H) for i, b in idmap.items()}
    doc_ids = set()   # element ids TOD (request 1) named as a paper -> drag-only, whatever the detector label says
    doc_of: dict = {}   # element id -> index of the paper it lies on
    dn_all = (facts or {}).get("docs_named") or []
    same_paper = paper_groups(dn_all)
    for i, b in idmap.items():   # name each paper by TOD's request-1 identity answer (geometry: centre inside)
        if b.kind in ("region", "background"):
            continue
        for j, d in enumerate(dn_all):
            x1, y1, x2, y2 = d["box"]
            if x1 <= b.center[0] <= x2 and y1 <= b.center[1] <= y2 and getattr(b, "name", "") not in layout.BY_NAME:
                rest = desc[str(i)].split(" — ", 1)[-1]   # drop the detector kind; TOD's identity names it
                if d["id"] == man.UNREAD:   # identity gate: no name below IDENTITY_MIN_P
                    wh = "counter" if d["where"] == "counter" else "desk"
                    desc[str(i)] = f"document on the {wh} — unread (TOD could not tell, p={d['p']:.2f}) — {rest}"
                else:
                    desc[str(i)] = f"{d['id']} (TOD {d['p']:.2f}) — {rest}"
                if nopp and d["id"] == "rulebook":
                    pass   # step N: rulebook pages and rule lines are clicked (page corner, inspect-mode rule)
                elif d["id"] != "other" or b.kind == "text":   # a texted 'other' paper is still a paper (run
                    doc_ids.add(i)                          # 090830 t22-43: the Pink Vice flyer clicked 8x)   # run 114927 t30-86: the counter passport was labelled 'rubber stamp' (click-only)
                    # a single-sheet paper's page corner is only another drag handle on it (passport: 29 of 93
                    # booth ticks offered panel + corner); multi-page papers keep their corner (a click turns a page)
                    if b.kind != "page_corner" or d["id"] not in MULTIPAGE_IDS:
                        doc_of[i] = same_paper[j]
                break
    banned_ids, ban_lines = {}, []
    if stuck is not None:
        for i, b in idmap.items():
            f = stuck.banned(tick, b)
            if f is not None:
                banned_ids[str(i)] = f
        for f in stuck.active_bans(tick):
            verb = "Clicking" if f.action == "click" else "Dragging"
            if f.why:
                ban_lines.append(f"{verb} '{short(f.desc, 60)}' {f.why}; excluded for {f.banned_until - tick} more "
                                 "tick(s)")
                continue
            ban_lines.append(f"{verb} '{short(f.desc, 60)}' did nothing (tried {f.count}x); excluded for "
                             f"{f.banned_until - tick} more tick(s)")
    # one drag source per paper (61% of booth ticks offered the same passport 2+ times): the largest box on it
    # (page corner, panel, icon and overlapping same-identity paper boxes count as one paper, paper_groups)
    dup_ids = set()
    for j in set(doc_of.values()):
        els = sorted((i for i, jj in doc_of.items() if jj == j),
                     key=lambda i: (idmap[i].kind == "page_corner", -idmap[i].area))
        dup_ids.update(str(i) for i in els[1:])
    # regions are drop targets only; the background is a click source only
    src_ids = {k: v for k, v in desc.items() if k not in banned_ids and idmap[int(k)].kind != "region"
               and k not in dup_ids}
    tgt_ids = {k: v for k, v in desc.items() if idmap[int(k)].kind != "background"} or dict(desc)
    if booth:   # in the booth every drag ends on a drop-target region (manual section 3); nothing else is a target
        tgt_ids = {k: v for k, v in tgt_ids.items() if idmap[int(k)].kind == "region"} or tgt_ids
    if booth:
        drop_desk = desk_target_redundant(idmap, src_ids, doc_ids, frame, state, day, facts)
        if drop_desk:
            tgt_ids = {k: v for k, v in tgt_ids.items() if k != drop_desk} or tgt_ids
        src_ids = cap_options(src_ids, tgt_ids, idmap, doc_ids, booth)
    region_ids = {k: idmap[int(k)].caption for k in desc if idmap[int(k)].kind == "region"}
    region_info = {}
    for k, cap in region_ids.items():
        name = next(n for n, c in REGION_CAPS.items() if c == cap)
        b = idmap[int(k)]
        region_info[name] = {"id": k, "box": [b.x1, b.y1, b.x2, b.y2], "target_source": region_src.get(name, "?")}
        if name in ("desk", "desk_clear") and desk_info is not None:
            region_info[name]["plan"] = {k_: desk_info[k_] for k_ in ("drop_native", "passport_box_planned",
                                                                       "passport_from", "passport_box_now", "check")}
    for name, v in region_src.items():
        if v == "dropped":   # vision extractor: could not be derived from this frame -> not offered (logged)
            region_info[name] = {"id": None, "box": None, "target_source": "dropped"}
    questions = build_questions(src_ids, tgt_ids, booth)
    # citation slips / flyers (TOD's identity) among the sources: their drop is checked in decide (never a stamp strip,
    # never the entrant -- a flyer only together with the entrant's papers after the passport went back)
    clutter_src = {k for k in src_ids if desc[k].split(" (TOD", 1)[0] in CLUTTER_IDS and int(k) in doc_ids}
    flyer_back = bool(facts and (facts.get("waiting_docs") or facts.get("handed_back") is not None))
    if facts is not None:
        facts["booth"] = booth
    state_text = man.build(state, history, day, ban_lines, facts)
    send = _small_for_send(annotated, args)
    url = encode_image(send, args.send_format, args.jpeg_quality)
    return dict(annotated=annotated, idmap=idmap, desc=desc, banned_ids=banned_ids, doc_ids=doc_ids, src_ids=src_ids,
                tgt_ids=tgt_ids, regions=region_info, booth=booth, questions=questions, state_text=state_text,
                image_url=url, image_kb=round(len(url) * 3 / 4 / 1024, 1), clutter_src=clutter_src,
                flyer_back=flyer_back,
                tray_flips=(facts or {}).get("tray_flips", 0), region_src=region_src,
                prep_ms=round((time.perf_counter() - t0) * 1e3, 1))


_PROBE_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="state-probe")
_LOG_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tick-log")


def _write_tick_images(stem: str, annotated, raw, raw_path: str, overlay, ov_args: tuple, ov_kw: dict) -> None:
    """Log thread: annotated + raw PNG and the overlay render of one tick (never read by the loop itself)."""
    try:
        cv2.imwrite(stem + ".png", annotated)
        if raw is not None:
            cv2.imwrite(raw_path, raw)
        if overlay is not None:
            overlay.render(annotated, *ov_args, **ov_kw)
    except Exception as e:
        print(f"[loop] tick log failed: {e}")
_DOC_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="doc-probe")


def _timed(fn, *a):
    t = time.perf_counter()
    r = fn(*a)
    return r, (time.perf_counter() - t) * 1e3


def _jdefault(o):
    """Tick logs: numpy scalars from the layout boxes -> Python numbers."""
    return o.item() if hasattr(o, "item") else str(o)


def _clean_state(state: dict) -> dict:
    return {k: {"value": v["value"], "p": v["p"], "probs": {a: round(b, 4) for a, b in (v.get("probs") or {}).items()}}
            for k, v in state.items()}


TRAY_FLIP_LIMIT = man.TRAY_FLIP_LIMIT
TRAY_BAN_TICKS = 4   # a toggle-looping closing tab stays excluded this long (A5)
NONBOOTH_STOP = 60   # step-7 ticks in a row (cutscenes, day_end, menus) before the run stops
REPEAT_DRAG_N = 4   # same drag source box (same place) N ticks running -> exclude the source
HORN_HIDE_P = 0.7
REPEAT_WINDOW, REPEAT_STOP = 12, 10   # same executed input 10 of the last 12 ticks -> stall stop (run 114927)    # person_at_window P(yes) above which the horn is not offered


def _is_tray_tab(b) -> bool:
    return b is not None and ("stamp tray tab" in (b.caption or "")
                              or getattr(b, "name", "") in ("tray_tab", "tray_tab_open"))


def fill_derived(res, D: dict) -> None:
    """Logs/overlay expect a 'target' answer; off the booth request 2 does not ask it. Insert a placeholder
    (marked derived=True). `action` is TOD's own answer (request 2)."""
    from types import SimpleNamespace
    if "target" not in res.answers:
        res.answers["target"] = SimpleNamespace(value="none", probabilities={"none": 1.0}, confidence=None,
                                                qtype="derived", derived=True)


def decide(res, P: dict) -> dict:
    """TOD's request-2 answers -> the input to perform. The input kind is TOD's `action` answer and the element is
    TOD's `source` pick (audit A5/B26): a mismatch with the element's click/drag convention (manual section 2) is
    logged in `note` / `convention_mismatch`, never corrected. Pure: no I/O."""
    src = str(res["source"].value)
    act = str(res["action"].value) if "action" in res.answers else None
    if src == WAIT_KEY or not src.isdigit() or act == "wait":
        why = "" if (src == WAIT_KEY or not src.isdigit()) else f"TOD action=wait (source #{src} not used)"
        return dict(action="wait", src=src, tgt="none", note=why, tod_pick=("wait", src), convention_mismatch=None,
                    p_src=float(res["source"].probabilities.get(src, 0.0)))
    cls = _cls(P["idmap"].get(int(src)), P["booth"], int(src) in P.get("doc_ids", ()))
    action = act if act in ("click", "drag") else ("drag" if cls == "drag" else "click")
    tod_pick = (action, src)
    note, mismatch = "", None
    if cls in ("click", "drag") and cls != action:
        mismatch = f"TOD chose {action} #{src}; by the convention that element is {cls}-only (executed as TOD chose)"
        note = mismatch
    if action == "drag" and "target" not in res.answers:
        return dict(action="wait", src=src, tgt="none", note="drag off the booth (no drop target) -> wait",
                    tod_pick=tod_pick, convention_mismatch=mismatch,
                    p_src=float(res["source"].probabilities.get(src, 0.0)))
    tgt = str(res["target"].value) if "target" in res.answers else "none"
    sb = P["idmap"].get(int(src)) if src.isdigit() else None
    tb_ = P["idmap"].get(int(tgt)) if tgt.isdigit() else None
    veto = None
    if (action == "drag" and sb is not None and tb_ is not None and tb_.kind == "region"
            and tb_.caption == REGION_CAPS["desk"] and not _is_tray_tab(sb)
            and tb_.x1 <= sb.center[0] <= tb_.x2 and tb_.y1 <= sb.center[1] <= tb_.y2):
        # the paper already lies on the desk target: executed as TOD chose (a no-op the stuck tracker sees), logged
        note = (note + "; " if note else "") + f"#{src} already lies on the desk target #{tgt} (executed as chosen)"
    if action == "drag" and src in P.get("clutter_src", ()) and tb_ is not None:
        # manual step K: a citation slip / flyer is never dropped under a stamp or on the tray edge, a citation never
        # on the entrant (a flyer only after the passport went back, G2). Such a drop is REFUSED (no input, logged);
        # TOD's target is never replaced by another one
        cid = P["desc"][src].split(" (TOD", 1)[0]
        bad = {REGION_CAPS["stamp_landing_denied"], REGION_CAPS["stamp_landing_approved"], REGION_CAPS["tray_stow"]}
        if not (cid == "flyer" and P.get("flyer_back")):
            bad.add(REGION_CAPS["hand_back"])
        if tb_.caption in bad:
            veto = f"refused: {cid} #{src} onto '{short(tb_.caption, 40)}' (step K: never stamped / handed back)"
    return dict(action=action, src=src, tgt=tgt, note=note, tod_pick=tod_pick, convention_mismatch=mismatch,
                veto=veto, p_src=float(res["source"].probabilities.get(src, 0.0)))


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
    waiting_docs: bool = False   # TOD: passport returned, person still there, papers of theirs still visible (G2)
    mark_side: dict | None = None   # {"value": 'approved'|'denied', "p", "tick"}: TOD's stamp-ink reading
    verdict: dict | None = None     # {"value": approved|denied|cannot_decide_yet, "p", "tick"}: TOD's verdict answer

    def reset(self, tick: int, why: str) -> None:
        self.country, self.stamp_clicks, self.handed_back, self.started = None, [], None, tick
        self.hb_drop, self.missed_stamps, self.checks, self.city, self.exp = None, [], {}, None, None
        self.tray_seen = []   # run 005956 t18/t21: entrant 2's tray toggles blocked entrant 3's first tray opening
        self.waiting_docs = False
        self.mark_side, self.verdict = None, None
        self.log.append((tick, why))
        print(f"           entrant memory reset ({why})")

    def observe(self, tick: int, state: dict, papers: bool = False, day: str = "1") -> None:
        """Start-of-tick update from request 1 (`papers`: TOD named a paper of the entrant's -- passport, ticket,
        flyer or an unread one -- on the desk or counter). The hand-back is TOD's PASSPORT_RETURNED_Q answer on this
        frame (audit B20), not a tick window."""
        person = man.yes(state, "person_at_window")
        ra = man.returned_answer(state)
        if ra == "returned" and person and self.handed_back is None and (
                self.hb_drop is not None or self.stamp_clicks or self.mark_side):
            # TOD: the passport is no longer here -> this entrant is done; the next one must not inherit the readings
            self.reset(tick, f"TOD: passport returned (drop at tick {self.hb_drop}, country="
                             f"{(self.country or {}).get('value')}, stamps={[sd for _, sd in self.stamp_clicks]})")
            self.handed_back = tick
        elif ra == "still_here":
            self.hb_drop = None   # TOD still sees the passport: the drop on the person was not a hand-back
            if self.handed_back is not None:
                self.handed_back, self.waiting_docs = None, False
        if self.handed_back is not None and person and ra == "returned":
            # G2 (run 070911 t79-111: the entrant waited for the ticket still on the desk)
            self.waiting_docs = bool(papers)
        if not person and (self.handed_back is not None or self.hb_drop is not None or not (
                man.yes(state, "document_open_on_desk") or man.yes(state, "document_on_counter_shelf"))):
            if self.country or self.stamp_clicks or self.handed_back is not None or self.hb_drop is not None:
                self.reset(tick, "window empty")
        if self.waiting_docs and man.yes(state, "document_open_on_desk", 0.6):
            # an open passport on the desk again = the next entrant's (run 113459 t82-92: G2 carried over to a new
            # entrant who arrived at 18:00)
            self.reset(tick, "open passport on the desk while waiting for papers: a new entrant")
        self.tray_seen = (self.tray_seen + [(tick, man.yes(state, "stamp_tray_open"))])[-9:]
        c = state.get("issuing_country")
        if (c and c["p"] >= CARRY_COUNTRY_P and c["value"] != "unreadable"
                and (self.country is None or c["p"] >= self.country["p"] or c["value"] == self.country["value"])):
            self.country = {"value": c["value"], "p": c["p"], "tick": tick}
        ci = state.get("issuing_city")
        if (ci and ci["p"] >= (CARRY_COUNTRY_P if ci["value"] in man._ALL_CITIES else man.DENY_P)
                and (self.city is None or ci["p"] >= self.city["p"] or ci["value"] == self.city["value"])):
            self.city = {"value": ci["value"], "p": ci["p"], "tick": tick}
        e = state.get("exp_read")   # TOD's pick among the OCR dates (request 1b)
        if e and e["p"] >= 0.5 and (self.exp is None or e["p"] >= self.exp["p"]):
            self.exp = {"value": e["value"], "p": e["p"], "tick": tick}
        ms = man.ink_now(state, {"stamp_clicks": self.stamp_clicks, "missed_stamps": self.missed_stamps})
        if ms:   # TOD's stamp-ink reading (STAMP_INK_Q) is the 'stamped' sign (audit B19)
            self.mark_side = {**ms, "tick": tick}
        v = state.get("verdict")
        if v and v["p"] >= man.VERDICT_P:
            self.verdict = {"value": v["value"], "p": v["p"], "tick": tick}
        for k in man.CHECK_KEYS:   # Day 2/3 checks, carried like the country (the page is hidden once under a stamp)
            v = man.check_answer(state.get(k))
            if v is not None:
                self.checks[k] = {"value": v, "raw": state[k]["value"], "from": state[k].get("from"),
                                  "p": state[k]["p"], "tick": tick}

    def after_action(self, tick: int, state: dict, action: str, sb, tb, frame, changed, src_desc: str = "") -> None:
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
            # the dropped paper must be the passport: TOD named it so, or it is an unnamed paper on the desk (run
            # 084642 t137: a counter paper TOD named 'rulebook' was dropped on the entrant right after the stamp, the
            # loop took it for the hand-back and the stamped passport stayed on the desk for 15 ticks)
            # a drop on the person is only a candidate; TOD's PASSPORT_RETURNED_Q answer next tick decides whether
            # the passport went back (observe), or the person leaving does
            self.hb_drop = tick

    def facts(self, tick: int, df: dict) -> dict:
        return {**df, "tick": tick, "country_carried": self.country, "stamp_clicks": list(self.stamp_clicks),
                "missed_stamps": list(self.missed_stamps), "handed_back": self.handed_back, "tray_flips": self.tray_flips(),
                "checks_carried": dict(self.checks), "city_carried": self.city, "exp_carried": self.exp,
                "waiting_docs": self.waiting_docs, "mark_side": self.mark_side, "hb_drop": self.hb_drop,
                "verdict_carried": self.verdict}

    def verdict_mem(self) -> dict:
        """TOD's own readings for this entrant, for the verdict question's text (manual.verdict_question)."""
        t = self.checks.get("entry_ticket_dated_today")
        tk = None
        if t:
            txt = {"dated_today": "VALID ON 1982.11.25 (today)", "other_date": "a date other than today",
                   "no_ticket": "no entry ticket among the papers"}.get(t.get("raw"), str(t.get("raw")))
            tk = {"value": (t.get("from") or txt), "p": t["p"], "tick": t["tick"]}
        return {"country": self.country, "exp": self.exp, "city": self.city, "ticket": tk}

    def tray_flips(self) -> int:
        """Open<->closed changes of the stamp tray over the last 8 ticks with no stamp click in between.
        Run 094930 t10-57: open, close, open, close ... 48 ticks (each toggle 'changed', so no stall stop)."""
        last_stamp = self.stamp_clicks[-1][0] if self.stamp_clicks else -1
        seq = [o for t, o in self.tray_seen if t > last_stamp]
        return sum(1 for a, b in zip(seq, seq[1:]) if a != b)


ENTRANT_PAPER_IDS = ("passport", "entry_ticket", "flyer", man.UNREAD)


def entrant_papers(state: dict, df: dict) -> bool:
    """TOD named (or could not name) a paper of the entrant's on the desk or counter (G2 'papers still here')."""
    return any(d["id"] in ENTRANT_PAPER_IDS for d in name_docs(state, df))


def set_verdict_ask(df: dict, ent: "Entrant", prev_state: dict | None, prev_facts: dict | None, day: str) -> None:
    """Request 1b asks TOD's verdict once the passport is readable: TOD read its country on an earlier tick, or last
    tick TOD saw an open passport on the desk / named a desk paper the passport -- and it was not handed back yet.
    The text carries TOD's own readings so far (Entrant.verdict_mem)."""
    ps, pf = prev_state or {}, prev_facts or {}
    seen = (ent.country is not None or man.yes(ps, "document_open_on_desk", INSPECT_OPEN_P)
            or any(d["where"] == "desk" and d["id"] == "passport" and d["p"] >= 0.5 for d in pf.get("docs_named") or []))
    df["ask_verdict"] = bool(seen and ent.handed_back is None and day in DAY_RULES)
    df["verdict_mem"] = ent.verdict_mem()


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
    seq = getattr(args, "sequential", False)
    # --sequential: consecutive frames of one run -> one entrant memory, history, exclusions and cycle guard carried
    # across them; each decision counts as executed and 'changed' (the next recorded frame is what followed)
    s_ent, s_hist, s_cyc = Entrant(), deque(maxlen=args.history), CycleDetector()
    s_stuck = StuckTracker(ban_ticks=args.ban_ticks)
    s_note, s_resets, s_prev, s_prev_f = ("", -1), 0, {}, {}
    for fi, path in enumerate(args.frames):
        m_t = re.search(r"(\d+)\.(?:png|jpg)$", path)
        otick = int(m_t.group(1)) if (seq and m_t) else fi
        frame = cv2.imread(path)
        if frame is None:
            print(f"[offline] cannot read {path}")
            continue
        t0 = time.perf_counter()
        boxes, vis, sinfo = get_boxes(frame, args.extractor)
        t_ex = (time.perf_counter() - t0) * 1e3
        df = desk_facts(boxes, frame.shape[1], frame.shape[0], sinfo)
        if not seq:
            DOC_CACHE.clear()   # offline frames are unrelated pictures
            INSP_CACHE.clear()
        df["tick"] = otick
        s_stuck.decay(otick)
        oday = args.day or "unknown"   # offline only: the day the frames come from (live runs read it via request 1)
        asked = ("issuing_country",) if oday == "1" else man.INSPECT_KEYS
        ctx = probe_context(s_prev_f, s_prev) if (seq and s_prev) else None
        probe, t_probe = _timed(state_probe, tod, frame, args, oday, asked, df, False, None,
                                (s_prev or None) if seq else None, ctx)   # identities ride in 1b, as live
        state = parse_state(probe)
        if seq:
            set_verdict_ask(df, s_ent, s_prev, s_prev_f, oday)
        else:
            df["ask_verdict"], df["verdict_mem"] = oday in DAY_RULES, {}
        dres, _ = _timed(doc_probe, tod, frame, args, df, oday)   # request 1b (live: parallel to request 1)
        if dres is not None:
            state.update(parse_state(dres))
        merge_doc_answers(state, df, otick)
        gate_inspection(state, asked, df)
        s_prev = state
        ent = s_ent if seq else Entrant()
        ent.observe(otick, state, papers=entrant_papers(state, df), day=oday)
        if seq and len(ent.log) != s_resets:
            s_resets = len(ent.log)
            s_cyc.reset()
        facts = ent.facts(otick, df)
        add_tod_facts(facts, state, df, sinfo)
        if seq and otick < s_note[1]:
            facts["cycle_note"] = s_note[0]
        if seq:
            P = prepare(frame, boxes, state, s_hist, oday, args, stuck=s_stuck, tick=otick, facts=facts)
        else:
            P = prepare(frame, boxes, state, deque(), oday, args, facts=facts)
        t1 = time.perf_counter()
        res = tod.ask(P["questions"], text=P["state_text"], image_data_url=P["image_url"])
        t_tod = (time.perf_counter() - t1) * 1e3
        D = decide(res, P)
        fill_derived(res, D)
        step = man.situation(state, oday if oday in DAY_RULES else "1", facts)
        s_prev_f = facts
        cyc_rec = None
        if seq:
            scr = state.get("screen", {}).get("value", "")
            sd = P["desc"].get(D["src"], "-")
            td = P["desc"].get(D["tgt"], "") if D["action"] == "drag" else ""
            sb = P["idmap"].get(int(D["src"])) if D["src"].isdigit() else None
            tb = P["idmap"].get(int(D["tgt"])) if D["tgt"].isdigit() else None
            ssum = man.state_summary(state, facts)
            if D["action"] == "drag":
                s_hist.append(f"t{otick} | {ssum} | drag | '{short(sd, 60)}' -> '{short(td, 50)}' | changed")
            elif D["action"] == "click":
                s_hist.append(f"t{otick} | {ssum} | click | '{short(sd, 60)}' | changed")
            else:
                s_hist.append(f"t{otick} | {ssum} | wait | - | -")
            ent.last_under = facts.get("passport_under") or []
            ent.after_action(otick, state, D["action"], sb, tb, frame, True, src_desc=sd)
            if len(ent.log) != s_resets:
                s_resets = len(ent.log)
                s_cyc.reset()
            hit = s_cyc.push(otick, cycle_signature(scr, state, step[0], D["action"], sd, td), cycle_progress(ent),
                             D["action"], sb, sd, td)
            if hit:
                note_c = cycle_callout(hit)
                for a, b_, sd_, _ in hit["actions"]:
                    if a in ("click", "drag") and b_ is not None and b_.kind != "background":
                        s_stuck.ban(otick, a, b_, sd_, 1, ticks=CYCLE_BAN_TICKS,
                                    why="was part of a repeating cycle with no progress")
                s_note = (note_c, otick + 1 + CYCLE_BAN_TICKS)
                cyc_rec = {"period": hit["period"], "n": hit["n"], "hits": hit["hits"], "callout": note_c,
                           "stop": "cycle" if hit["hits"] >= 2 else None}
                print(f"[offline] t{otick} cycle_break: {note_c}" + ("  -> stop reason: cycle" if hit["hits"] >= 2 else ""))
        r = {
            "frame": path, "tick": otick, "cycle_break": cyc_rec, "excluded": sorted(P["banned_ids"], key=int),
            "verdict": state.get("verdict"), "verdict_carried": facts.get("verdict_carried"),
            "verdict_asked": bool(df.get("ask_verdict")),
            "stamps_offered": sorted(_stamp_side(P["idmap"][int(k)], frame) for k in P["src_ids"]
                                     if _stamp_side(P["idmap"][int(k)], frame)),
            "tray_open_px": facts.get("tray_open_px"), "stamped": man.stamped(state, facts),
            "strip_source": {k: v.get("source") for k, v in (facts.get("strip") or {}).items()},
            "unread_docs": [d["where"] for d in facts.get("docs_named") or [] if d["id"] == man.UNREAD],
            "extractor": args.extractor, "passport_under": facts.get("passport_under"),
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
            json.dump(r, fh, indent=1, default=_jdefault)
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
    try:   # TOD's own last day reading, carried across a loop restart within the hour (run 101823-102817: after a
        # restart on Day 3 the booth readout 82.11.25 was read as '2' at 0.51-0.59 and Day 3 rules never applied)
        with open(LAST_DAY_FILE, encoding="utf-8") as fh:
            ld = json.load(fh)
        if time.time() - ld["time"] < 3600 and ld["day"] in DAY_RULES and not args.dry_run:
            day = ld["day"]
            print(f"[loop] day {day} carried from {LAST_DAY_FILE} (read at tick {ld.get('tick')} of {ld.get('run')})")
    except (OSError, ValueError, KeyError):
        pass
    last_state: dict = {}
    last_facts: dict = {}   # previous tick's facts: chooses which request-1 questions can matter
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
    nonbooth_n = 0   # consecutive step-7 (non-booth screen) ticks
    screen_seq: list[str] = []
    recent_inputs: deque = deque(maxlen=REPEAT_WINDOW)
    menu_bounces = 0
    ent = Entrant()
    pick_key, pick_n, refused_n = None, 0, 0   # --pick-stop / --refuse-stop counters (per entrant)
    drag_key, drag_n = None, 0                  # repeat-drag rule (REPEAT_DRAG_N)
    cyc = CycleDetector()                       # generic period-2/3 cycle guard (per entrant)
    cycle_note: tuple = ("", -1)                # (callout for the history block, shown until tick)
    n_resets = 0                                # len(ent.log) last seen -> entrant-scoped counters reset
    unreach_n = 0                               # --unreachable-stop counter (consecutive)
    bad4xx_n = 0                                # consecutive TOD 4xx (bad request) ticks -> stop at 2

    def park_cursor():
        if not args.dry_run and is_foreground(hwnd):
            io_win.move(hwnd, *park)

    # --pause-think (harness timing, not a TOD decision): the game's own pause menu (Esc) is opened after the
    # frame is grabbed and closed again before TOD's input is executed, so the game clock does not run while
    # extraction and the TOD requests are in flight. TOD only ever sees the frame grabbed BEFORE the pause.
    pause = {"on": False, "t": 0.0, "pre": None}

    def pause_game(rec: dict, pre: np.ndarray) -> None:
        if args.dry_run or not args.pause_think or pause["on"] or not try_foreground(hwnd):
            return
        io_win.key(io_win.VK_ESCAPE)
        t0 = time.perf_counter()
        frac = 0.0
        while time.perf_counter() - t0 < 1.0:   # the menu has to be visible, else this tick runs unpaused
            time.sleep(0.08)
            frac = changed_frac(change_map(pre, grab.grab()))
            if frac >= PAUSE_MIN_FRAC:
                break
        pause.update(on=frac >= PAUSE_MIN_FRAC, t=time.perf_counter(), pre=pre)
        rec["pause"] = {"opened": pause["on"], "menu_frac": round(frac, 4),
                        "ms": round((time.perf_counter() - t0) * 1e3)}
        print(f"[pause] Esc -> pause menu {'shown' if pause['on'] else 'NOT seen'} (changed {frac:.3f})")
        if not pause["on"]:   # whatever Esc did, undo it so no menu is left for the next frame
            io_win.key(io_win.VK_ESCAPE)
            time.sleep(0.3)

    def resume_game(rec: dict | None) -> None:
        if not pause["on"]:
            return
        held = time.perf_counter() - pause["t"]
        frac = 1.0
        for attempt in range(2):
            try_foreground(hwnd)
            io_win.key(io_win.VK_ESCAPE)
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < 1.2:   # back to the booth: the frame matches the pre-pause one again
                time.sleep(0.08)
                frac = changed_frac(change_map(pause["pre"], grab.grab()))
                if frac < PAUSE_MIN_FRAC:
                    break
            if frac < PAUSE_MIN_FRAC:
                break
        pause["on"] = False
        if rec is not None:
            rec.setdefault("pause", {}).update(resumed=frac < PAUSE_MIN_FRAC, held_s=round(held, 2),
                                               resume_frac=round(frac, 4), resume_attempts=attempt + 1)
        print(f"[pause] Esc -> resumed after {held:.1f} s (frame back: {frac < PAUSE_MIN_FRAC}, {frac:.3f})")

    try:
        for tick in range(args.max_ticks):
            rec: dict = {"tick": tick, "time": time.time()}
            row: dict = {"tick": tick}
            rows.append(row)
            stuck.decay(tick)
            resume_game(None)   # a tick that ended early (skip / stop paths) left the menu open
            if args.stop_file and os.path.exists(args.stop_file):   # clean external stop (the game is not left paused)
                os.remove(args.stop_file)
                stop_reason = f"stop file {args.stop_file}"
                print(f"[loop] STOP: {stop_reason}")
                break
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
            fam = screen_family(frame)
            rec["screen_family"] = list(fam)
            if args.pause_think and fam[0] == "booth":
                pause_game(rec, frame)

            # ---- REQUEST 1 (state, unmarked frame) in parallel with extraction; then 1b (new papers only) -----
            # Day 1 decides on the country only; expiry/photo are asked from Day 2 (frees 2 of TOD's 16 questions)
            asked = ("issuing_country",) if day == "1" else man.INSPECT_KEYS
            pctx = probe_context(last_facts, last_state) if last_state else None
            pfacts = {"tick": tick}
            fut = _PROBE_POOL.submit(_timed, state_probe, tod, frame, args, day, asked, pfacts, False, fam,
                                     last_state or None, pctx)
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
            set_verdict_ask(df, ent, last_state, last_facts, day)   # TOD's verdict question rides in request 1b
            rec["desk_facts"], rec["inspect_asked"] = df, list(asked)
            try:
                dfut = (_DOC_POOL.submit(_timed, doc_probe, tod, frame, args, df, day)
                        if fam[0] == "booth" else None)
                probe, rec["state_ms"] = fut.result()
                state = parse_state(probe)
                dres, rec["doc_ms"] = dfut.result() if dfut is not None else (None, 0.0)
                if dres is not None:
                    state.update(parse_state(dres))
                merge_doc_answers(state, df, tick)
                rec["docs_cached"] = sum(1 for d in df.get("docs") or [] if d.get("cached"))
                rec["q_dropped"] = pfacts.get("q_dropped") or []
                if df.get("insp_reuse"):
                    rec["insp_reused_from"] = next(iter(df["insp_reuse"].values()))["reused_from"]
            except TodCreditExhausted as e:  # 402: every later call fails too -> stop once, do not skip ticks
                stop_reason = "TOD credit exhausted (402)"
                print(f"[tick {tick:03d}] {e}")
                print(f"[loop] STOP: {stop_reason}")
                rec.update(tod_error=f"state: {e}", executed="none (TOD credit exhausted)", stop_reason=stop_reason)
                row.update(action="none", effect="stop: " + stop_reason)
                with open(os.path.join(run_dir, f"tick_{tick:04d}.json"), "w", encoding="utf-8") as fh:
                    json.dump(rec, fh, indent=1, default=_jdefault)
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
                    json.dump(rec, fh, indent=1, default=_jdefault)
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
                if day != dv["value"] and not args.dry_run:
                    with open(LAST_DAY_FILE, "w", encoding="utf-8") as fh:
                        json.dump({"day": dv["value"], "time": time.time(), "tick": tick, "run": run_dir}, fh)
                day = dv["value"]   # days only move forward (run 015649 t46-52: Day 2 booth frames read '1' at 0.45)
            if screen == "day_end" and (ent.country or ent.stamp_clicks or ent.handed_back is not None):
                # the day is over (Day 2 ends at the bombing mid-entrant): nothing of this entrant carries into the next day
                ent.reset(tick, "day_end screen")
            ent.observe(tick, state, papers=entrant_papers(state, df), day=day)
            if len(ent.log) != n_resets:   # new entrant: stall / pick / refusal counters are entrant-scoped
                n_resets = len(ent.log)
                refused_n, pick_key, pick_n, stall_key, stall_n, drag_key, drag_n = 0, None, 0, None, 0, None, 0
                cyc.reset()
            facts = ent.facts(tick, df)
            add_tod_facts(facts, state, df, sinfo)
            if tick < cycle_note[1]:
                facts["cycle_note"] = cycle_note[0]
            rec["docs_named"] = [{k: d[k] for k in ("where", "id", "p", "text")} for d in facts["docs_named"]]
            rec["strip"] = facts["strip"]
            rec["entrant"] = {"country": ent.country, "city": ent.city, "exp": ent.exp, "checks": dict(ent.checks), "stamp_clicks": list(ent.stamp_clicks),
                              "missed_stamps": list(ent.missed_stamps), "handed_back": ent.handed_back,
                              "mark_side": ent.mark_side, "verdict": ent.verdict, "hb_drop": ent.hb_drop}
            rec["clutter"] = facts.get("clutter")
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
            # step 7 (cutscene / day_end / menu screens: the Day 2 bombing ends the day mid-entrant) repeats the same
            # click on purpose; the stall / pick / repeat / cycle stops skip it (bounded by NONBOOTH_STOP instead)
            nonbooth = step[0] == "7"
            nonbooth_n = nonbooth_n + 1 if nonbooth else 0
            if nonbooth_n >= NONBOOTH_STOP:
                stop_reason = f"non-booth screen {screen} for {nonbooth_n} ticks running"
            stall_key, stall_n = key, (stall_n + 1 if key == stall_key and not nonbooth else 1)
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
                    json.dump(rec, fh, indent=1, default=_jdefault)
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
                    json.dump(rec, fh, indent=1, default=_jdefault)
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
                    json.dump(rec, fh, indent=1, default=_jdefault)
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
                print(f"[tick {tick:03d}] note: {D['note']}")
            rec.update(
                state_text=P["state_text"],
                descriptions=desc,
                excluded={k: v.desc for k, v in banned_ids.items()},
                boxes={str(i): b.to_dict() for i, b in idmap.items()},
                answers={q: {"choice": a.value, "probabilities": a.probabilities, "confidence": a.confidence}
                         for q, a in res.answers.items()},
                tod_pick=list(D["tod_pick"]), input_convention=D["note"] or None,
                convention_mismatch=D.get("convention_mismatch"),
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
            veto = D.get("veto")   # step K drop refused in decide (no input, TOD's target never replaced)
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
            resume_game(rec)
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
                _sp = strip_drop_point(sb, tb, facts, frame, state)
                if _sp is not None:
                    (bx, by) = _sp
                    rec["strip_drop"] = [bx, by]
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
            last_state, last_facts = state, facts
            if action == "drag" and sb is not None and tb is not None and not veto and executed.startswith("drag"):
                dk = (src_desc, tb.caption or desc.get(tgt, ""), ssum)
                # the same source box at the same place again = the drags are not moving it; the target and the
                # state summary may flicker (loop10 run 161058 t131-153: tray-hidden passport -> DENIED/APPROVED
                # strips 23x, step D/E? alternating, never banned)
                same = drag_key is not None and _same_element(drag_key[3], sb)
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
                if n_top >= REPEAT_STOP and top_in not in ("wait",) and not nonbooth:
                    stop_reason = f"stalled: '{top_in}' {n_top} times in the last {REPEAT_WINDOW} ticks"
            # hard stall stops: same manual step + same TOD pick N ticks running; stamp press refused M times
            pk = (step[0], D["tod_pick"][0], short(desc.get(D["tod_pick"][1], D["tod_pick"][1]), 60))
            pick_key, pick_n = pk, (pick_n + 1 if pk == pick_key and not nonbooth else 1)
            if args.pick_stop and pick_n >= args.pick_stop:
                stop_reason = f"stalled: manual step {pk[0]} + TOD pick {pk[1]} '{pk[2]}' {pick_n} ticks running"
            if veto and veto.startswith("refused") and D["tod_pick"][1] == src:
                # counted only when TOD itself picked that stamp (run 005956 t4: a convention re-pick landed on it)
                refused_n += 1
                if args.refuse_stop and refused_n >= args.refuse_stop:
                    stop_reason = f"stalled: stamp press refused {refused_n} times"
            ent.last_under = facts.get("passport_under") or []
            rec["passport_under"] = ent.last_under
            ent.after_action(tick, state, action, sb, tb, frame, changed,
                             src_desc=desc.get(str(src), "") if src is not None else "")
            if len(ent.log) != n_resets:
                n_resets = len(ent.log)
                refused_n, pick_key, pick_n, stall_key, stall_n, drag_key, drag_n = 0, None, 0, None, 0, None, 0
                cyc.reset()
            # ---- generic cycle guard (A,B,A,B / A,B,C,A,B,C with no entrant progress) --------------------------
            ex_act = "veto" if veto else (action if executed.startswith(("click", "drag")) else "wait")
            tgt_d = desc.get(tgt, "") if action == "drag" else ""
            hit = cyc.push(tick, cycle_signature(screen, state, step[0], ex_act, src_desc, tgt_d),
                           cycle_progress(ent), ex_act, sb, src_desc, tgt_d) if not nonbooth else None
            if hit:
                note_c = cycle_callout(hit)
                banned_c = []
                for a, b_, sd, _ in hit["actions"]:
                    if a in ("click", "drag") and b_ is not None and b_.kind != "background":
                        stuck.ban(tick, a, b_, sd, 1, ticks=CYCLE_BAN_TICKS, why="was part of a repeating cycle "
                                  "with no progress")
                        banned_c.append(sd)
                cycle_note = (note_c, tick + 1 + CYCLE_BAN_TICKS)
                rec["cycle_break"] = {"period": hit["period"], "n": hit["n"], "hits": hit["hits"],
                                      "actions": [[a, sd, td] for a, _, sd, td in hit["actions"]],
                                      "excluded": banned_c, "callout": note_c}
                print(f"           cycle: {note_c} -> {len(banned_c)} source(s) excluded for {CYCLE_BAN_TICKS} ticks")
                if hit["hits"] >= 2 and not stop_reason and not nonbooth:
                    stop_reason = (f"cycle: second period-{hit['period']} cycle for this entrant "
                                   f"({'; '.join(short(_desc_key(sd), 40) for _, _, sd, _ in hit['actions'])})")

            # ---- log -----------------------------------------------------------------
            # the JSON is written now; the PNGs and the overlay render (~0.5-0.75 s) go to the log thread, which the
            # next tick does not wait for (the arrays are this tick's and are not changed afterwards)
            stem = os.path.join(run_dir, f"tick_{tick:04d}")
            with open(stem + ".json", "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=1, default=_jdefault)
            _LOG_POOL.submit(_write_tick_images, stem, annotated, frame if args.save_raw else None,
                             os.path.join(run_dir, f"raw_{tick:04d}.png"), overlay,
                             (idmap, res, os.path.join(run_dir, f"viz_{tick:04d}.png")),
                             dict(descriptions=desc, executed=executed, effect=effect, state_line=sline,
                                  chosen=(action, src, tgt), note=D["note"]))
    finally:
        _LOG_POOL.submit(lambda: None).result()   # pending tick images are on disk before the summary
        try:
            resume_game(None)
        except Exception as e:
            print(f"[pause] resume at exit failed: {e}")
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
    ap.add_argument("--sequential", action="store_true",
                    help="offline: the frames are consecutive ticks of one run -- carry entrant memory, history, "
                         "exclusions and the cycle guard across them (each decision counts as executed)")
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
    ap.add_argument("--stop-file", default=os.path.join("runs", "STOP_LOOP"), help="stop cleanly at the next tick "
                    "when this file exists (it is deleted); use instead of killing the process under --pause-think")
    ap.add_argument("--pause-think", action="store_true", help="harness timing: open the game's pause menu (Esc) "
                    "after the frame grab, close it before the input; TOD only sees the pre-pause frame (off by default)")
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
