"""Static layout of the Papers, Please screens (Day 1-3 booth + menus) and a
cheap, model-free element extractor built on it.

Papers, Please renders 570x320 pixel art scaled by an integer factor (4x at
2280x1280), and nearly every control sits at a fixed place. This module keeps
those places as a table measured offline from saved frames (runs/*/raw_*.png,
OpenCV only, no TOD), plus three cheap tests:

  * screen_of(native)    -- 'booth' / 'title' / 'day_select' / 'button_screen' /
                            'other', from a fixed booth probe (roof + wall vs
                            a reference booth frame) and binary template matches
                            of the menu buttons at their fixed slots;
  * tray_open(native)    -- red DENIED + green APPROVED stamp bodies at their
                            open-tray positions;
  * find_documents(...)  -- documents on the desk (bright connected components on
                            the near-black desk, split by paper colour) and on the
                            counter (difference vs the empty reference counter).

extract_static(frame_bgr) returns extract.Box objects (subclass LBox with the
layout name and the input affordance) in FRAME pixels, so the loop can use it
in place of extract.extract(). No GPU, no models; optional rapidocr text on the
found documents only (ocr=True / TOD_STATIC_OCR=1).

Coordinates below are NATIVE [x1, y1, x2, y2] (570x320) and are scaled to the
frame / client rect at runtime (scale_box). Elements marked measured=False were
not visible in any saved frame (Day 4+ controls, pause menu); they are nominal
and their visibility tests keep them off until measured.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass

import cv2
import numpy as np

from .extract import Box

NATIVE_W, NATIVE_H = 570, 320
ASSETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "layout_assets.npz")


@dataclass
class LBox(Box):
    """extract.Box plus the layout element name and its input affordance
    ('click' | 'drag' | 'target' | 'read' | 'avoid')."""
    name: str = ""
    affordance: str = ""


@dataclass(frozen=True)
class Element:
    name: str
    kind: str            # extract.Box kind: 'object' | 'icon' | 'text' | 'region' | 'panel'
    desc: str            # what TOD sees as the element's criterion text
    affordance: str      # click | drag | target | read | avoid
    box: tuple           # native [x1, y1, x2, y2]
    when: str            # visibility condition, see _visible()
    measured: bool = True


# --------------------------------------------------------------------------
# the table
# --------------------------------------------------------------------------

# fixed menu slots (also used by tools/build_layout_assets.py to cut templates)
MENU_SLOTS = {
    "story": (230, 239, 339, 254),
    "endless": (230, 263, 339, 278),
    "quit": (543, 10, 560, 27),
    "settings": (10, 281, 39, 310),
    "select_header": (157, 11, 412, 27),
    # only the inner dashed "NEW" box of a day tile takes the click; the "DAY 1" label above it does not
    # (run 20261002_110614: 6 clicks at native y=73 did nothing; earlier runs clicked y=90 and it worked)
    "day1_tile": (6, 74, 74, 104),
    "trash": (527, 285, 556, 310),
    "bottom_button": (220, 293, 349, 308),   # BACK / NEXT / WALK TO WORK (same slot)
}

LAYOUT: list[Element] = [
    # ---------------- booth, always (Day 1-3) ----------------
    Element("horn", "object", "loudspeaker horn on the booth roof -- click it to call the next entrant ('Next!')",
            "click", (164, 64, 184, 80), "booth"),
    Element("shutter_lever", "object", "shutter lever at the top-right corner of the booth window -- drag it "
            "down to open or close the window shutter", "drag", (150, 103, 178, 133), "booth"),
    Element("rulebook", "object", "rulebook lying in its slot below the counter -- drag it onto the desk to "
            "open it", "drag", (112, 277, 140, 313), "booth+rulebook_in_slot"),
    Element("bulletin", "object", "bulletin storage below the counter -- drag the bulletin onto the desk to "
            "read it", "drag", (40, 278, 65, 313), "booth"),
    Element("transcript", "object", "audio transcript printer below the counter -- drag the printed "
            "transcript onto the desk to read it", "drag", (76, 269, 102, 298), "booth"),
    Element("clock", "text", "clock below the counter (time of day; not clickable)", "read",
            (7, 286, 27, 307), "booth"),
    Element("date", "text", "date readout below the counter (today's date; not clickable)", "read",
            (2, 308, 36, 319), "booth"),
    Element("counter_shelf", "region", "counter shelf under the window -- drop documents here (onto the "
            "hand-back slot) to hand them back", "target", (10, 212, 172, 266), "booth"),
    Element("desk", "region", "desk (drop documents here to read them)", "target", (196, 230, 340, 304), "booth"),
    # rulebook slot below the counter: an open rulebook dropped here goes back into its slot (run 024712/030xxx:
    # the rulebook lay open over the desk and was only ever moved around on the desk)
    Element("stow_papers", "region", "rulebook slot below the counter -- drop the rulebook here to put it away",
            "target", (106, 272, 146, 316), "booth"),
    # the person behind the glass (head + chest); documents dropped here are handed back (run 092612: dropped on
    # the counter shelf they just lay there). Measured on runs 083908 raw_0000 / 090556 raw_0022.
    Element("hand_back", "region", "the entrant at the booth window -- drop documents ON THE PERSON to hand them "
            "back", "target", (45, 135, 135, 205), "booth"),
    # ---------------- stamp tray ----------------
    Element("tray_tab", "object", "stamp tray tab at the right edge of the desk -- drag it left onto the "
            "desk to pull out the stamp tray", "drag", (558, 128, 570, 177), "booth+tray_closed"),
    Element("tray_tab_open", "object", "stamp tray tab (left end of the open stamp bar) -- drag it right to "
            "put the tray away", "drag", (284, 135, 298, 180), "booth+tray_open"),
    # whole stamp, knob + body (the game stamps on a click anywhere on it; run 101448 t11 clicked the knob)
    Element("stamp_denied", "object", "red DENIED stamp (knob and body) on the open tray -- click it to stamp "
            "the passport lying in the strip beneath it", "click", (330, 106, 406, 174), "booth+tray_open"),
    Element("stamp_approved", "object", "green APPROVED stamp (knob and body) on the open tray -- click it to "
            "stamp the passport lying in the strip beneath it", "click", (450, 106, 526, 174), "booth+tray_open"),
    Element("landing_denied", "region", "stamp landing strip (under the DENIED stamp head)", "target",
            (340, 180, 400, 218), "booth+tray_open"),
    Element("landing_approved", "region", "stamp landing strip (under the APPROVED stamp head)", "target",
            (460, 180, 520, 218), "booth+tray_open"),
    Element("tray_stow", "region", "right edge of the desk (drag the tray tab here to put the stamp tray away)",
            "target", (530, 135, 568, 175), "booth+tray_open"),
    # ---------------- later-day controls (not seen on Day 1-3 frames) ----------------
    Element("inspect_toggle", "object", "red inspect-mode button at the lower right of the desk -- click it "
            "to enter or leave inspect mode", "click", (535, 280, 565, 315), "booth+inspect_button", False),
    Element("scanner", "object", "scanner / fingerprint panel on the left booth wall", "click",
            (0, 125, 12, 180), "never", False),
    # ---------------- menus ----------------
    Element("story", "text", "STORY menu button -- click to start or continue the story", "click",
            MENU_SLOTS["story"], "title"),
    Element("endless", "text", "ENDLESS menu button (endless mode; not used)", "avoid", MENU_SLOTS["endless"], "title"),
    Element("quit_title", "icon", "quit-game button (top right; never click)", "avoid", MENU_SLOTS["quit"], "title"),
    Element("day1_tile", "object", "day tile 'DAY 1 - NEW' (top-left) -- a tile, not a button: click it to start "
            "Day 1", "click", MENU_SLOTS["day1_tile"], "day_select+tile1"),
    Element("day2_tile", "object", "Day 2 tile -- click to continue from Day 2", "click", (86, 74, 154, 104),
            "day_select+tile2", False),
    Element("day3_tile", "object", "Day 3 tile -- click to continue from Day 3", "click", (166, 74, 234, 104),
            "day_select+tile3", False),
    # BACK only ever leads back to the title menu (run 20261002_113830 bounced menu <-> day select 15 ticks);
    # it is never offered on the day-select screen
    Element("back", "text", "BACK button (returns to the previous menu)", "click", MENU_SLOTS["bottom_button"],
            "btn:back+!day_select"),
    Element("trash", "icon", "delete-save (trash) button (never click)", "avoid", MENU_SLOTS["trash"], "day_select"),
    Element("next", "text", "NEXT button -- click to continue", "click", MENU_SLOTS["bottom_button"], "btn:next"),
    Element("walk_to_work", "text", "WALK TO WORK button -- click to go to the booth", "click",
            MENU_SLOTS["bottom_button"], "btn:walk_to_work"),
    Element("continue", "text", "Continue (pause menu)", "click", (235, 140, 335, 156), "never", False),
    Element("quit_pause", "text", "Quit (pause menu; never click)", "avoid", (235, 164, 335, 180), "never", False),
]
BY_NAME = {e.name: e for e in LAYOUT}

# booth probes (native)
ROOF = (146, 86, 200, 96)       # roof edge above the window (below the horn, which animates)
WALL = (178, 100, 570, 103)     # thin strip of the booth wall above the desk
BOOTH_DIFF_MAX = 0.05           # fraction of probe px off by > 40 grey levels
DESK = (178, 103, 570, 320)
TRAY_BAR = (288, 133, 570, 212)  # open stamp bar + its shadow (docs show only above it)
COUNTER = (0, 211, 178, 276)
DOC_MIN_AREA = 120
KNOB_X = ((331, 404), (451, 524))   # stamp knob columns above TRAY_BAR when open (native px)
# the dark strip under the stamp bar where a document must lie to be stamped, per stamp head (native)
DESK_LABEL = (326, 305, 436, 318)   # 'DRAG DOCUMENTS HERE' text printed on the desk (native)
STRIP_Y = (194, 212)
STRIP_X = {"denied": (335, 401), "approved": (455, 521)}
STRIP_PAPER_FRAC = 0.45   # paper pixels (max channel > 100) in a strip -> a document lies under that head
TPL_MIN_IOU = 0.6


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

_assets = None


def _A() -> dict:
    global _assets
    if _assets is None:
        z = np.load(ASSETS_FILE)
        _assets = {k: z[k] for k in z.files}
    return _assets


def to_native(frame_bgr: np.ndarray) -> np.ndarray:
    """Frame -> 570x320 native art (exact stride sampling at integer scales)."""
    H, W = frame_bgr.shape[:2]
    s = W // NATIVE_W
    if s >= 1 and W == s * NATIVE_W and H == s * NATIVE_H:
        return np.ascontiguousarray(frame_bgr[::s, ::s])
    return cv2.resize(frame_bgr, (NATIVE_W, NATIVE_H), interpolation=cv2.INTER_AREA)


def scale_box(b, W: int, H: int) -> tuple[int, int, int, int]:
    """Native box -> frame/client pixels for a W x H frame or client rect."""
    sx, sy = W / NATIVE_W, H / NATIVE_H
    x1, y1, x2, y2 = b
    return int(round(x1 * sx)), int(round(y1 * sy)), int(round(x2 * sx)), int(round(y2 * sy))


def _crop(n: np.ndarray, b) -> np.ndarray:
    x1, y1, x2, y2 = b
    return n[y1:y2, x1:x2]


def _diff(n: np.ndarray, ref: np.ndarray, b) -> int:
    return int(np.abs(_crop(n, b).astype(np.int16) - _crop(ref, b).astype(np.int16)).max())


def _tpl_score(n: np.ndarray, name: str, slot: str) -> float:
    """Best IoU of the binary template vs the current mask at its slot (+-1 px)."""
    tpl = _A().get("tpl_" + name)
    if tpl is None:
        return 0.0
    x1, y1, x2, y2 = MENU_SLOTS[slot]
    t = tpl.astype(bool)
    best = 0.0
    for dy in (0, -1, 1):
        for dx in (0, -1, 1):
            if y1 + dy < 0 or x1 + dx < 0:
                continue
            c = n[y1 + dy:y2 + dy, x1 + dx:x2 + dx]
            if c.shape[:2] != t.shape:
                continue
            m = c.max(2) > 40
            u = (m | t).sum()
            if u:
                best = max(best, float((m & t).sum()) / u)
    return best


# --------------------------------------------------------------------------
# screen tests
# --------------------------------------------------------------------------

def booth_score(n: np.ndarray) -> float:
    """Fraction of the roof + wall probe pixels that differ from the reference booth."""
    ref = _A()["ref_booth"]
    bad = tot = 0
    for b in (ROOF, WALL):
        d = np.abs(_crop(n, b).astype(np.int16) - _crop(ref, b).astype(np.int16)).max(2) > 40
        bad += int(d.sum())
        tot += d.size
    return bad / tot


def screen_of(n: np.ndarray) -> tuple[str, dict]:
    """('booth' | 'title' | 'day_select' | 'button_screen' | 'other', flags)."""
    flags: dict = {}
    if booth_score(n) <= BOOTH_DIFF_MAX:
        return "booth", flags
    if _tpl_score(n, "story", "story") >= TPL_MIN_IOU and _tpl_score(n, "endless", "endless") >= TPL_MIN_IOU:
        return "title", flags
    for name in ("next", "walk_to_work", "back"):
        if _tpl_score(n, name, "bottom_button") >= TPL_MIN_IOU:
            flags["btn:" + name] = True
            break
    if _tpl_score(n, "select_header", "select_header") >= TPL_MIN_IOU:
        # a tile is offered only when it is drawn (an off-screen / not yet painted region captures black)
        for i, el in ((1, "day1_tile"), (2, "day2_tile"), (3, "day3_tile")):
            flags[f"tile{i}"] = bool((_crop(n, BY_NAME[el].box).max(2) > 40).mean() > 0.02)
        return "day_select", flags
    return ("button_screen" if flags else "other"), flags


def _red(c: np.ndarray) -> np.ndarray:
    c = c.astype(np.int16)
    b, g, r = c[..., 0], c[..., 1], c[..., 2]
    return (r > 80) & (r > g + 50) & (r > b + 50)


def _green(c: np.ndarray) -> np.ndarray:
    c = c.astype(np.int16)
    b, g, r = c[..., 0], c[..., 1], c[..., 2]
    return (g > 70) & (g > r + 15) & (g > b + 40)


def tray_open(n: np.ndarray) -> bool:
    """Open-tray signature: red DENIED and green APPROVED stamp bodies in place."""
    return bool(_red(_crop(n, (340, 145, 395, 170))).mean() > 0.3
                and _green(_crop(n, (460, 145, 515, 170))).mean() > 0.3)


def passport_under(n: np.ndarray) -> list[str]:
    """Stamp heads ('denied', 'approved') with a document lying in the strip beneath them (native frame, tray
    open). The desk there is near-black; paper (cream passport, grey visa) is bright. The rulebook's dark blue
    cover scores ~0.3 and is not counted."""
    out = []
    y1, y2 = STRIP_Y
    for side, (x1, x2) in STRIP_X.items():
        c = n[y1:y2, x1:x2].astype(np.int16)
        # tinted paper only: the bulletin is exactly neutral grey/white (120,120,120 / 240,240,240) and must not
        # count (run 20261002_112327: 13 DENIED presses on the bulletin); passport paper is tinted (108,112,118)
        paper = (c.max(2) > 100) & ((c.max(2) - c.min(2)) >= 6)
        if float(paper.mean()) >= STRIP_PAPER_FRAC:
            out.append(side)
    return out


def _visible(when: str, screen: str, flags: dict) -> bool:
    for cond in when.split("+"):
        if cond == "never":
            return False
        if cond.startswith("!"):
            if screen == cond[1:] or flags.get(cond[1:]):
                return False
            continue
        if cond in ("booth", "title", "day_select"):
            if screen != cond:
                return False
        elif cond.startswith("btn:") or cond.startswith("tile"):
            if not flags.get(cond):
                return False
        elif not flags.get(cond):
            return False
    return True


# --------------------------------------------------------------------------
# document finder (no models)
# --------------------------------------------------------------------------

def _paperish(c: np.ndarray) -> np.ndarray:
    mx, mn = c.max(2).astype(np.int16), c.min(2).astype(np.int16)
    return (mx > 150) & (mx - mn < 80)


def _components(mask: np.ndarray, min_area: int) -> list[list[int]]:
    n, _, st, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    return [[int(x), int(y), int(x + w), int(y + h)] for x, y, w, h, a in st[1:]
            if a >= min_area and w >= 6 and h >= 6]


def _merge_strip(boxes: list[list[int]], top: int, gap: int = 36) -> list[list[int]]:
    """Paper peeking above the open tray is cut into pieces by the stamp knobs:
    merge pieces that touch the bar's top edge and are < gap apart."""
    edge = sorted([b for b in boxes if b[3] >= top - 8], key=lambda b: b[0])
    rest = [b for b in boxes if b[3] < top - 8]
    out: list[list[int]] = []
    for b in edge:
        if out and b[0] - out[-1][2] <= gap:
            o = out[-1]
            out[-1] = [min(o[0], b[0]), min(o[1], b[1]), max(o[2], b[2]), max(o[3], b[3])]
        else:
            out.append(list(b))
    return rest + out


def _split_by_paper(n: np.ndarray, b: list[int]) -> list[list[int]]:
    """A component can be two overlapping documents (entry visa on the rulebook):
    the paper colours inside it (quantised to 24 levels) are separate sheets when
    one colour fills the component and another is a large sub-rectangle of it."""
    x1, y1, x2, y2 = b
    c = n[y1:y2, x1:x2]
    pm = _paperish(c)
    if pm.sum() < 200:
        return [b]
    q = (c // 24).astype(np.int32)
    key = (q[..., 0] * 64 + q[..., 1]) * 64 + q[..., 2]
    vals, cnt = np.unique(key[pm], return_counts=True)
    order = np.argsort(-cnt)
    area = (x2 - x1) * (y2 - y1)
    subs = []
    for i in order[:4]:
        if cnt[i] < 0.08 * area:
            break
        ys, xs = np.nonzero((key == vals[i]) & pm)
        sb = [x1 + int(xs.min()), y1 + int(ys.min()), x1 + int(xs.max()) + 1, y1 + int(ys.max()) + 1]
        subs.append(sb)
    if len(subs) < 2:
        return [b]
    full = [s for s in subs if (s[2] - s[0]) * (s[3] - s[1]) >= 0.8 * area]
    if not full:
        return [b]   # e.g. the two pages of one open passport: keep the component
    out = [b]
    for s in subs:
        sa = (s[2] - s[0]) * (s[3] - s[1])
        if s in full or sa < 0.12 * area or sa > 0.7 * area:
            continue
        if all(_iou4(s, o) < 0.6 for o in out):
            out.append(s)
    return out


def _iou4(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - ix * iy
    return ix * iy / u if u else 0.0


def find_documents(n: np.ndarray, is_tray_open: bool | None = None) -> list[dict]:
    """Documents on the desk and the counter, native boxes:
    [{'where': 'desk'|'counter', 'box': [x1,y1,x2,y2]}]."""
    if is_tray_open is None:
        is_tray_open = tray_open(n)
    docs: list[dict] = []
    # desk: the desk is near-black (max channel <= 50, incl. 'DRAG DOCUMENTS HERE')
    x0, y0, x1_, y1_ = DESK
    d = n[y0:y1_, x0:x1_]
    m = d.max(2) > 60
    if is_tray_open:
        bx1, by1, bx2, by2 = TRAY_BAR
        m[by1 - y0:by2 - y0, bx1 - x0:bx2 - x0] = False
        # stamp knobs/bodies stick up above the bar; keep only paper-bright pixels there
        for kx1, kx2 in KNOB_X:
            k = d[:by1 - y0, kx1 - x0:kx2 - x0].max(2) < 190
            m[:by1 - y0, kx1 - x0:kx2 - x0][k] = False
    else:
        tx1, ty1, tx2, ty2 = BY_NAME["tray_tab"].box
        m[ty1 - y0:ty2 - y0, tx1 - x0 - 2:tx2 - x0] = False
    m = cv2.morphologyEx(m.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    comps = [[a + x0, b + y0, c + x0, e + y0] for a, b, c, e in _components(m, DOC_MIN_AREA)]
    if is_tray_open:
        comps = _merge_strip(comps, TRAY_BAR[1])
    # a knob-cut fragment lying inside another document's box is part of it
    comps = [b for i, b in enumerate(comps) if not any(
        j != i and o[0] <= b[0] and o[1] <= b[1] and b[2] <= o[2] + 1 and b[3] <= o[3]
        and (o[2] - o[0]) * (o[3] - o[1]) > (b[2] - b[0]) * (b[3] - b[1]) for j, o in enumerate(comps))]
    for b in comps:
        for s in _split_by_paper(n, b):
            docs.append({"where": "desk", "box": s, "under_tray": is_tray_open and s[3] >= TRAY_BAR[1] - 2
                         and s[1] >= DESK[1] and s[0] >= TRAY_BAR[0] - 2})
    # counter: difference vs the empty reference counter
    ref = _A()["ref_booth"]
    cx1, cy1, cx2, cy2 = COUNTER
    diff = np.abs(_crop(n, COUNTER).astype(np.int16) - _crop(ref, COUNTER).astype(np.int16)).max(2) > 30
    for b in _components(diff, 60):
        docs.append({"where": "counter", "box": [b[0] + cx1, b[1] + cy1, b[2] + cx1, b[3] + cy1]})
    return docs


_DOC_DESC = {
    "desk": "document on the desk -- drag it (onto a stamp landing strip to stamp it, onto the person at the "
            "window to hand it back)",
    "desk_under_tray": "document under the open stamp tray (only its top edge shows) -- drag it out onto the "
                       "desk, or it is already beneath the stamps",
    "counter": "document on the counter shelf -- drag it onto the desk to read it",
}


_OCR_CACHE: dict = {}   # doc crop hash -> text: a document lying still is read once


def _ocr_docs(frame_bgr: np.ndarray, n: np.ndarray, boxes: list[LBox]) -> None:
    """rapidocr (CPU) on each document crop only; fills Box.text."""
    from . import extract as ex
    s = frame_bgr.shape[1] / NATIVE_W
    si = max(1, int(round(s)))
    for b in boxes:
        x1, y1, x2, y2 = (int(b.x1 / s), int(b.y1 / s), int(b.x2 / s), int(b.y2 / s))
        crop = np.ascontiguousarray(n[y1:y2, x1:x2])
        if crop.shape[0] < 8 or crop.shape[1] < 8:
            continue
        key = hashlib.blake2b(crop.tobytes(), digest_size=12).digest() + bytes(crop.shape[:2])
        if key not in _OCR_CACHE:
            try:
                lines = ex._ocr_boxes(crop, si)
            except Exception:
                return
            lines.sort(key=lambda t: (t.y1, t.x1))
            if len(_OCR_CACHE) > 64:
                _OCR_CACHE.pop(next(iter(_OCR_CACHE)))
            _OCR_CACHE[key] = " | ".join(t.text for t in lines if t.text)[:400]
        b.text = _OCR_CACHE[key]


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------

LAST: dict = {}


def extract_static(frame_bgr: np.ndarray, targets: bool = True, informational: bool = False,
                   ocr: bool | None = None) -> list[Box]:
    """Layout elements visible on this frame + found documents, as LBox in frame
    pixels. targets=False drops drop-target regions (the loop derives its own);
    informational=True keeps read-only text (clock, date) and 'avoid' items."""
    t0 = time.perf_counter()
    if ocr is None:
        ocr = os.environ.get("TOD_STATIC_OCR", "0") == "1"
    H, W = frame_bgr.shape[:2]
    n = to_native(frame_bgr)
    screen, flags = screen_of(n)
    if screen == "booth":
        ref = _A()["ref_booth"]
        flags["tray_open"] = tray_open(n)
        flags["tray_closed"] = not flags["tray_open"]
        flags["rulebook_in_slot"] = _diff(n, ref, (114, 280, 138, 311)) <= 30
        flags["inspect_button"] = int(_red(_crop(n, BY_NAME["inspect_toggle"].box)).sum()) > 40
        flags["passport_under"] = passport_under(n) if flags["tray_open"] else []
    out: list[Box] = []
    for e in LAYOUT:
        if not _visible(e.when, screen, flags):
            continue
        if e.affordance in ("read", "avoid") and not informational:
            continue
        if e.affordance == "target" and not targets:
            continue
        x1, y1, x2, y2 = scale_box(e.box, W, H)
        out.append(LBox(x1, y1, x2, y2, "", e.kind, 1.0, caption=e.desc, name=e.name, affordance=e.affordance))
    docs = []
    if screen == "booth":
        docs = find_documents(n, flags["tray_open"])
        for i, d in enumerate(docs):
            key = "desk_under_tray" if d.get("under_tray") else d["where"]
            x1, y1, x2, y2 = scale_box(d["box"], W, H)
            out.append(LBox(x1, y1, x2, y2, "", "panel", 0.9, caption=_DOC_DESC[key],
                            name=f"doc_{d['where']}_{i}", affordance="drag"))
        if ocr:
            t = time.perf_counter()
            _ocr_docs(frame_bgr, n, [b for b in out if getattr(b, "name", "").startswith("doc_")])
            flags["ocr_ms"] = round((time.perf_counter() - t) * 1e3, 1)
    LAST.clear()
    LAST.update(screen=screen, flags=flags, docs=docs, n_docs=len(docs), ms=round((time.perf_counter() - t0) * 1e3, 2))
    return out




_MENU_NAMES = {"story", "day1_tile", "day2_tile", "day3_tile"}


def merge_hybrid(static_boxes: list[Box], vision_boxes: list[Box], iou_drop: float = 0.3,
                 frame_wh: tuple[int, int] | None = None, screen: str | None = None) -> list[Box]:
    """Static fixed-layout elements (never missed) + vision's boxes for everything
    else (document panels with identity/text, per-paper splits). A vision box
    overlapping a static fixed element (IoU >= iou_drop) is dropped. Static's own
    document boxes are kept only when vision found nothing on desk/counter."""
    def iou(a, b):
        ix = max(0, min(a.x2, b.x2) - max(a.x1, b.x1))
        iy = max(0, min(a.y2, b.y2) - max(a.y1, b.y1))
        i = ix * iy
        u = (a.x2 - a.x1) * (a.y2 - a.y1) + (b.x2 - b.x1) * (b.y2 - b.y1) - i
        return i / u if u > 0 else 0.0

    def inside(a, b):   # fraction of a lying inside b
        ix = max(0, min(a.x2, b.x2) - max(a.x1, b.x1))
        iy = max(0, min(a.y2, b.y2) - max(a.y1, b.y1))
        return ix * iy / a.area if a.area > 0 else 0.0

    fixed = [b for b in static_boxes if not getattr(b, "name", "").startswith("doc")]
    docs = [b for b in static_boxes if getattr(b, "name", "").startswith("doc")]
    objs = [f for f in fixed if f.kind != "region"]
    # a vision box overlapping a fixed element, or lying mostly inside a fixed object (the stamp knob, the
    # APPROVED/DENIED text on a stamp body, the horn) is the same control: keep only the static one
    keep = [v for v in vision_boxes if all(iou(v, f) < iou_drop for f in fixed)
            and all(inside(v, f) < 0.6 for f in objs)]
    if screen in ("title", "day_select") or any(getattr(f, "name", "") in _MENU_NAMES for f in fixed):
        # title / day-select: the static layout has every control; vision's boxes there (icon captions on the
        # black screen, the BACK text the layout deliberately does not offer) are dropped
        keep = []
    if frame_wh is not None and any(getattr(f, "name", "") == "horn" for f in fixed):
        # booth: vision is only asked for the DOCUMENTS (desk + counter shelf). Its boxes on the yard, the person,
        # the drawer row, the stamp bar and the tray label are clutter next to the static controls.
        W, H = frame_wh
        sx, sy = W / NATIVE_W, H / NATIVE_H
        tray = any(getattr(f, "name", "") == "tray_tab_open" for f in fixed)
        bar = Box(int(TRAY_BAR[0] * sx), int(TRAY_BAR[1] * sy), int(TRAY_BAR[2] * sx), int(TRAY_BAR[3] * sy))
        label = Box(*scale_box(DESK_LABEL, W, H), "", "region", 0.0)   # 'DRAG DOCUMENTS HERE' printed on the desk

        def doc_area(v) -> bool:
            cx, cy = v.center[0] / sx, v.center[1] / sy
            if DESK[0] <= cx <= DESK[2] and DESK[1] <= cy <= DESK[3]:
                return True
            # on the counter shelf only a document-sized box (a passport is ~40x45 native px), not the shelf
            return (COUNTER[0] <= cx <= COUNTER[2] and COUNTER[1] <= cy <= COUNTER[3]
                    and v.w / sx <= 70 and v.h / sy <= 70 and v.w / sx >= 15 and v.h / sy >= 15)

        keep = [v for v in keep if doc_area(v)
                and not inside(v, label) >= 0.6
                and not (tray and inside(v, bar) >= 0.8)]
    vdocs = [v for v in keep if any(iou(v, d) > 0.1 for d in docs)]
    return fixed + keep + ([] if vdocs else docs)
