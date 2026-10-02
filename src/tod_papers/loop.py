"""loop.py -- the TOD Set-of-Mark agent loop.

Each tick:  wait-until-stable grab -> extract -> som -> ONE TOD request -> execute
            -> verify (frame diff, global + localized at source/target) -> log.

TOD makes every decision: what kind of input (click/drag/wait), which numbered
element, which drop target, and what screen we're on. This file contains no
game coordinates and no scripted navigation; the only geometry it uses is the
centre of whichever box TOD picked. The loop only *removes* options that were
empirically shown to do nothing (stuck blacklist) or that are unsafe.

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

# tight per-day briefs (docs/game.md section 5, days 1-3)
DAY_RULES = {
    "1": "Day 1 (Nov 23 1982): only the passport is required. Only citizens of Arstotzka may enter; deny every foreigner. The first entrant is a tutorial (approve).",
    "2": "Day 2 (Nov 24): passport only. Foreigners with a valid passport may now enter; documents must be up to date (deny expired); check photo and passport fields.",
    "3": "Day 3 (Nov 25): foreigners need passport + entry ticket dated Nov 25 (any other date -> deny). Citizens need passport only.",
}

# how the booth works (docs/game.md sections 3-4), no coordinates
BOOTH_BRIEF = (
    "How the booth works: the loudspeaker/horn on the booth calls the next entrant; the shutter lever "
    "beside the booth window opens/closes the window. The entrant puts documents on the counter "
    "below the booth window; documents must be dragged onto the desk to read them. Multi-page papers "
    "turn at their page corner. The stamp tray slides out from the tab at the desk edge; to stamp, drag "
    "the open passport under the tray and click APPROVED or DENIED. Then drag all documents back onto "
    "the counter / entrant to return them. Objects on the desk are moved by dragging, not clicking."
)

OBJECTIVE = (
    "You are controlling the game Papers, Please through mouse input. Objective: play the Story "
    "campaign, advance through intro/cutscene/bulletin screens, and then work the border booth: "
    "call entrants, inspect their documents against the day's rules, and stamp approve/deny "
    "correctly. Clickable elements in the screenshot carry numbered markers; you choose by number. "
    "Grey struck-through markers are temporarily unavailable."
)

# neutral one-liners: what the inspector is working toward, given the last screen judgement
AIMS = {
    "menu": "Getting from the menus into the story campaign.",
    "day_select": "Choosing where to continue the story from.",
    "cutscene_or_text": "Moving past a story / text screen.",
    "bulletin": "Finishing with the day's bulletin and getting to work in the booth.",
    "booth_idle": "Nobody is being processed right now; the next entrant needs to come to the booth.",
    "documents_on_desk": "An entrant is being processed: documents onto the desk, check against the rules, stamp the passport, return everything.",
    "stamp_tray_open": "Stamps are available; the passport's visa page needs to be under a stamp for a stamp to land.",
    "inspect_mode": "Comparing details for a discrepancy, or leaving inspect mode.",
    "day_end": "Wrapping up the day and continuing to the next.",
    "other": "Working out what the screen is and how to continue.",
}

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


BG_DESC = ("no element - empty space at the screen centre (click it to advance cutscene/text screens "
           "that have no button; in the booth this is free desk space to drop things on)")
# captions that say nothing useful about what an element is; left out of the "visible:" summary
_NOISE_CAPS = {"dark empty background", "unlabelled graphic"}


def describe(b: Box, W: int, H: int) -> str:
    """Criteria text for one box. extract.describe() is the single source (kind,
    OCR text or caption, coarse position); the loop only adds its synthetic
    'background' option."""
    if b.kind == "background":
        return BG_DESC
    return ex.describe(b, W, H)


def _pl(word: str) -> str:
    if "/" in word:
        return "/".join(_pl(w) for w in word.split("/"))
    if word.endswith(("s", "x", "ch", "sh")):
        return word + "es"
    if word.endswith("y") and not word.endswith(("ay", "ey", "oy")):
        return word[:-1] + "ies"
    return word + "s"


def _plural(cap: str, n: int) -> str:
    if n == 1:
        return cap
    head, sep, tail = cap.partition(" (")
    return f"{n} {_pl(head)}{sep}{tail}"


def visible_summary(boxes) -> str:
    """Neutral inventory of the labelled things on screen, e.g. 'lever handle,
    2 rubber stamps, passport booklet, 5 text lines'. Alphabetical, no ranking."""
    caps: dict[str, int] = {}
    n_text = 0
    for b in boxes:
        if b.kind == "background":
            continue
        if b.text and b.kind != "page_corner" and b.caption not in ex.DOC_CAPS:
            n_text += 1
            continue
        c = (b.caption or "").strip()
        if not c or c.startswith("possibly ") or c in _NOISE_CAPS:
            continue
        if c.startswith("page corner"):
            c = "page corner"
        caps[c] = caps.get(c, 0) + 1
    parts = [_plural(c, n) for c, n in sorted(caps.items())]
    if n_text:
        parts.append(_plural("text line", n_text))
    return ", ".join(parts) if parts else "(nothing labelled)"


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


SOURCE_STYLES = ("default", "short", "goal")


SCREEN_PROBE_TEXT = "A screenshot of the game Papers, Please."


def screen_probe(tod: TodClient, frame: np.ndarray, args):
    """Separate short request: plain (unmarked) frame + one-line text + the
    screen question. Asked inside the long agent prompt over the marked frame,
    `screen` was near-uniform and wrong on 3/4 saved frames; asked like this it
    was right on 4/4. Runs concurrently with extract(), so it costs no latency."""
    W, H = frame.shape[1], frame.shape[0]
    small = frame
    if args.send_width and W > args.send_width:
        small = cv2.resize(frame, (args.send_width, int(H * args.send_width / W)), interpolation=cv2.INTER_AREA)
    q = {"screen": choice("Which kind of screen is currently shown?", dict(SCREENS))}
    return tod.ask(q, text=SCREEN_PROBE_TEXT, image_data_url=encode_image(small, args.send_format, args.jpeg_quality))


def source_instruction(style: str, aim: str) -> str:
    if style == "short":
        return "Which numbered element should be used next?"
    if style == "goal":
        return f"{aim} Which numbered element should be clicked (or, for a drag, picked up) for that?"
    return "Which numbered element should be clicked (or, for a drag, picked up)? Use the number drawn on the marker."


def current_aim(last_screen: str | None, last_goal: float | None) -> str:
    aim = AIMS.get(last_screen, "Working out what the screen is and how to continue.")
    if last_screen in ("documents_on_desk", "stamp_tray_open") and last_goal is not None and last_goal >= 0.6:
        aim = "The current entrant looks done; the next entrant needs to come to the booth."
    return aim


def build_questions(src_ids: dict[str, str], all_ids: dict[str, str], source_instr: str | None = None,
                    aim: str = "") -> dict:
    # the action question is goal-conditioned like the source question: asked
    # bare, TOD answered "click" 0.61 on the entrant frame although every
    # document must be dragged (scratchpad loop4/dryrun2.md)
    return {
        "action": choice(
            (f"{aim} " if aim else "") + "Look at the annotated screenshot and recent history. What kind of mouse input should be performed next to make progress toward the objective?",
            {
                "click": "click one numbered element (button, menu option, text/cutscene to advance, stamp, speaker)",
                "drag": "drag one numbered element (e.g. a document, or a lever) onto another numbered element or empty space",
                "wait": "do nothing this tick (a transition/animation is in progress or nothing is actionable)",
            },
        ),
        "source": choice(
            source_instr or source_instruction("default", ""),
            src_ids,
        ),
        # no "none" option: it took 0.15-0.24 on every frame and a drag with
        # target none is skipped; the target is only used when action == drag
        "target": choice(
            (f"{aim} " if aim else "") + "If the next input is a drag: where should the dragged item end up - "
            "onto which numbered element, or empty space? Not the item itself. (Ignored when the input is a click.)",
            dict(all_ids),
        ),
        "day": choice(
            "Which in-game day is it (from bulletin, date or clock text if visible; otherwise unknown)?",
            {"1": "Day 1 / Nov 23", "2": "Day 2 / Nov 24", "3": "Day 3 / Nov 25", "later": "Day 4 or later", "unknown": "not determinable yet"},
        ),
        "goal": noul(
            "Has the current entrant been fully processed (passport stamped and documents returned)? "
            "If no entrant has been called yet, answer no.",
            "yes - stamped and documents handed back",
            "no - not yet (or no entrant processed yet)",
        ),
    }


def build_state_text(history: deque, day: str, aim: str, ban_lines: list[str], visible: str = "") -> str:
    rules = DAY_RULES.get(day)
    rule_txt = rules if rules else "Current day not yet known. Rules by day: " + " | ".join(DAY_RULES.values())
    hist = "\n".join(f"- {h}" for h in history) if history else "- (none yet; this is the first action)"
    parts = [OBJECTIVE, f"Rules: {rule_txt}", BOOTH_BRIEF]
    if visible:
        parts.append(f"Labelled on screen now (automatic detection, may be imperfect): {visible}.")
    parts += [f"Current aim: {aim}", f"Last actions (oldest first):\n{hist}"]
    if ban_lines:
        parts.append("Ruled out for now:\n" + "\n".join(f"- {s}" for s in ban_lines))
    return "\n\n".join(parts)


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
    lines += ["", "| tick | screen | action | source desc | p(src) | goal p(yes) | effect |",
              "|---:|---|---|---|---:|---:|---|"]
    for r in rows:
        d = str(r.get("src_desc", "-")).replace("|", "/")
        if r.get("tgt_desc"):
            d += " -> " + str(r["tgt_desc"]).replace("|", "/")
        goal = r.get("goal")
        lines.append(f"| {r['tick']} | {r.get('screen', '-')} | {r.get('action', '-')} | {short(d, 90)} | "
                     f"{r.get('p_src', 0):.2f} | {'-' if goal is None else f'{goal:.2f}'} | {r.get('effect', '-')} |")
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


# screens on which "empty space" is only a drop destination, never something to click/pick up
BOOTH_SCREENS = {"booth_idle", "documents_on_desk", "stamp_tray_open", "inspect_mode"}


def prepare(frame: np.ndarray, boxes: list[Box], aim: str, history, day: str, args,
            stuck: "StuckTracker | None" = None, tick: int = 0, screen: str = "") -> dict:
    """Everything between extract() and the TOD request: Set-of-Mark annotation,
    criteria (extract.describe), the visible-objects summary, questions, state
    text and the encoded (downscaled) image."""
    t0 = time.perf_counter()
    H, W = frame.shape[:2]
    banned = (lambda b: stuck.banned(tick, b) is not None) if stuck is not None else None
    annotated, idmap = annotate(frame, boxes, max_marks=args.max_marks - 1, excluded=banned)
    # always offer "the screen itself" so text/cutscene screens can be advanced
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
    src_ids = {k: v for k, v in desc.items() if k not in banned_ids}
    if screen in BOOTH_SCREENS:  # TOD's own screen judgement (probe)
        src_ids = {k: v for k, v in src_ids.items() if idmap[int(k)].kind != "background"}
    visible = visible_summary(idmap.values())
    questions = build_questions(src_ids, desc, source_instruction(args.source_style, aim),
                                aim if args.source_style == "goal" else "")
    state_text = build_state_text(history, day, aim, ban_lines, visible)
    send = annotated
    if args.send_width and W > args.send_width:
        send = cv2.resize(annotated, (args.send_width, int(H * args.send_width / W)), interpolation=cv2.INTER_AREA)
    url = encode_image(send, args.send_format, args.jpeg_quality)
    return dict(annotated=annotated, idmap=idmap, desc=desc, banned_ids=banned_ids, src_ids=src_ids,
                visible=visible, questions=questions, state_text=state_text, image_url=url,
                image_kb=round(len(url) * 3 / 4 / 1024, 1), prep_ms=round((time.perf_counter() - t0) * 1e3, 1))


_PROBE_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="screen-probe")


def _timed(fn, *a):
    t = time.perf_counter()
    r = fn(*a)
    return r, (time.perf_counter() - t) * 1e3


def offline(args) -> int:
    """--frames: extract -> prepare -> one TOD request per saved frame, each with
    fresh state (no history). Never looks for the game window, never sends input."""
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
        fut = _PROBE_POOL.submit(_timed, screen_probe, tod, frame, args)
        boxes = ex.extract(frame)
        t_ex = (time.perf_counter() - t0) * 1e3
        probe, t_probe = fut.result()
        t_wait = (time.perf_counter() - t0) * 1e3 - t_ex
        screen = probe["screen"].value
        P = prepare(frame, boxes, current_aim(screen, None), deque(), "unknown", args, screen=screen)
        t1 = time.perf_counter()
        res = tod.ask(P["questions"], text=P["state_text"], image_data_url=P["image_url"])
        t_tod = (time.perf_counter() - t1) * 1e3
        t_total = (time.perf_counter() - t0) * 1e3
        r = {
            "frame": path, "n_boxes_raw": len(boxes), "n_options": len(P["src_ids"]),
            "extract_ms": round(t_ex), "extract_timings": {k: round(v) for k, v in ex.LAST_TIMINGS.items()},
            "prep_ms": P["prep_ms"], "image_kb": P["image_kb"], "tod_ms": round(t_tod),
            "probe_ms": round(t_probe), "probe_wait_after_extract_ms": round(t_wait), "total_ms": round(t_total),
            "aim": current_aim(screen, None), "source_instructions_used": P["questions"]["source"]["instructions"],
            "visible": P["visible"], "criteria": P["src_ids"], "state_text": P["state_text"],
            "source_instructions": P["questions"]["source"]["instructions"],
            "answers": {q: {"choice": a.value, "probabilities": a.probabilities} for q, a in res.answers.items()},
            "source_top3": [(k, P["desc"].get(k, k), p) for k, p in top(res["source"].probabilities)],
            "screen_top3": top(probe["screen"].probabilities), "action_top3": top(res["action"].probabilities),
            "target_top3": top(res["target"].probabilities),
        }
        results.append(r)
        stem = os.path.join(out_dir, os.path.basename(os.path.dirname(os.path.abspath(path))) + "_"
                            + os.path.splitext(os.path.basename(path))[0])
        cv2.imwrite(stem + "_som.png", P["annotated"])
        with open(stem + ".json", "w", encoding="utf-8") as fh:
            json.dump(r, fh, indent=1)
        print(f"[offline] {path}: options={r['n_options']} extract={t_ex:.0f}ms probe={t_probe:.0f}ms "
              f"(+{max(0, t_wait):.0f}ms wait) prep={P['prep_ms']:.0f}ms img={P['image_kb']}KB tod={t_tod:.0f}ms "
              f"total={t_total:.0f}ms screen={r['screen_top3']} action={r['action_top3']}")
        for k, d, p in r["source_top3"]:
            print(f"           source #{k} p={p:.3f} {d}")
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

    history: deque = deque(maxlen=6)
    stuck = StuckTracker(limit=args.stuck_limit, ban_ticks=args.ban_ticks)
    rows: list[dict] = []
    day = "unknown"
    last_screen = None
    last_goal = None
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

            t0 = time.perf_counter()
            fut = _PROBE_POOL.submit(_timed, screen_probe, tod, frame, args)
            boxes = ex.extract(frame)
            rec["extract_ms"] = round((time.perf_counter() - t0) * 1e3, 1)
            rec["extract_timings"] = {k: round(v, 1) for k, v in getattr(ex, "LAST_TIMINGS", {}).items()}
            probe = None
            try:
                probe, rec["probe_ms"] = fut.result()
                screen = probe["screen"].value
                rec["screen_probs"] = probe["screen"].probabilities
            except RuntimeError as e:  # probe failed: fall back to last tick's judgement
                print(f"[tick {tick:03d}] screen probe failed: {e}")
                screen = last_screen or "other"
            rec["probe_wait_ms"] = round((time.perf_counter() - t0) * 1e3 - rec["extract_ms"], 1)
            aim = current_aim(screen, last_goal)
            P = prepare(frame, boxes, aim, history, day, args, stuck=stuck, tick=tick, screen=screen)
            annotated, idmap, desc, banned_ids = P["annotated"], P["idmap"], P["desc"], P["banned_ids"]
            questions, state_text = P["questions"], P["state_text"]
            rec["prep_ms"] = P["prep_ms"]
            t1 = time.perf_counter()
            try:
                res = tod.ask(questions, text=state_text, image_data_url=P["image_url"])
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
            if probe is not None:  # keep logs/overlay shaped as before: screen alongside the other answers
                res.answers["screen"] = probe["screen"]

            action = res["action"].value
            src = str(res["source"].value)
            tgt = str(res["target"].value)
            if action == "drag" and tgt == src:
                # dropping an item on itself is a no-op: take TOD's best other target
                alt = [k for k, _ in sorted(res["target"].probabilities.items(), key=lambda kv: -kv[1]) if k != src]
                if alt:
                    tgt = alt[0]
            day_ans = res["day"].value
            p_goal = p_true(res["goal"])
            p_src = float(res["source"].probabilities.get(src, 0.0))
            if day_ans in DAY_RULES and res["day"].probabilities.get(day_ans, 0) >= 0.5:
                day = day_ans
            last_screen, last_goal = screen, p_goal

            rec.update(
                state_text=state_text,
                descriptions=desc,
                excluded={k: v.desc for k, v in banned_ids.items()},
                boxes={str(i): b.to_dict() for i, b in idmap.items()},
                answers={q: {"choice": a.value, "probabilities": a.probabilities, "confidence": a.confidence}
                         for q, a in res.answers.items()},
            )
            sb = idmap.get(int(src)) if src.isdigit() else None
            tb = idmap.get(int(tgt)) if tgt.isdigit() else None
            src_desc = desc.get(src, "-")
            print(
                f"[tick {tick:03d}] boxes={len(idmap)} wait={waited:.2f}s extract={rec['extract_ms']:.0f}ms tod={rec['tod_ms']:.0f}ms "
                f"screen={screen} aim={short(aim, 40)!r} action={top(res['action'].probabilities)} "
                f"source={top(res['source'].probabilities)} ({short(src_desc)}) "
                f"target={top(res['target'].probabilities, 2)} day={day_ans} goal={p_goal:.2f}"
                + (f" excluded={sorted(banned_ids, key=int)}" if banned_ids else "")
            )
            row.update(screen=screen, action=action, src_desc=src_desc, p_src=p_src, goal=p_goal)
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

            label = f"'{short(src_desc, 60)}'" if sb else "-"
            eff = "" if changed is None else (" -> changed" if changed else " -> NO change")
            if veto:
                history.append(f"tick {tick} [{screen}]: {veto}")
            elif action == "click":
                history.append(f"tick {tick} [{screen}]: clicked {label}{eff}")
            elif action == "drag":
                tl = f"'{short(desc.get(tgt, 'nothing'), 50)}'"
                history.append(f"tick {tick} [{screen}]: dragged {label} onto {tl}{eff}")
            else:
                history.append(f"tick {tick} [{screen}]: waited")

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
                                   descriptions=desc, executed=executed, effect=effect)
                except Exception as e:
                    print(f"[loop] overlay failed: {e}")
    finally:
        grab.close()
        write_summary(run_dir, rows, {
            "ticks": len(rows), "TOD calls": tod.total_calls, "cost": f"${tod.total_cost:.4f}",
            "dry_run": args.dry_run, "args": " ".join(sys.argv[1:]),
        })
        print(f"[loop] done. TOD calls={tod.total_calls} cost=${tod.total_cost:.4f}. Logs: {run_dir}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="TOD Set-of-Mark agent loop for Papers, Please")
    ap.add_argument("--max-ticks", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true", help="do everything except mouse input")
    ap.add_argument("--max-marks", type=int, default=25, help="option cap incl. the background option (soft: text, "
                    "page corners and detector-labelled objects are never dropped)")
    ap.add_argument("--send-format", choices=("png", "jpeg"), default="png", help="png: smaller than jpeg on pixel art (~80 vs ~170 KB)")
    ap.add_argument("--jpeg-quality", type=int, default=90)
    ap.add_argument("--source-style", choices=SOURCE_STYLES, default="goal",
                    help="wording of the source question: default | short | goal (prefixed with the current aim)")
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
