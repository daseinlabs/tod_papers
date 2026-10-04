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
import re
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
    # the same shelf as a CLICK target while nothing lies on it (pixel test vs the empty reference counter) and the
    # inspect button exists (Day 2+): in inspect mode the empty counter is clicked after the rule that requires the
    # missing document (docs/game.md, missing-document interrogation). The TAS clicks inside this box.
    Element("counter_empty", "object", "counter shelf -- empty (nothing lies on it); in inspect mode click it after a "
            "rulebook rule to point out a missing document", "click", (10, 212, 172, 266),
            "booth+inspect_button+counter_empty"),
    # rulebook slot below the counter: an open rulebook dropped here goes back into its slot (run 024712/030xxx:
    # the rulebook lay open over the desk and was only ever moved around on the desk)
    # run 044332 t35-38: drops on the drawer row (106-146, 272-316) did nothing; papers dragged left off the desk
    # onto the counter shrink to their closed form there
    Element("stow_papers", "region", "counter shelf left of the desk -- drop the rulebook, bulletin or a flyer here to put "
            "it away (it closes and leaves the desk)", "target", (20, 222, 110, 262), "booth"),
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


TILE_BAND = (0, 36, NATIVE_W, 112)     # day-select: the row of day tiles (labels + their click boxes), native
DAY_LABEL_RE = re.compile(r"^\s*D\s*[A4]\s*[YVy]?\s*(\d{1,2})?\s*$", re.I)


def day_tiles(n: np.ndarray) -> list[dict]:
    """Day-select screen: every drawn day tile, read by OCR (loop14). Each "DAY" label the detector finds in the
    tile band is one tile; its number is recognised from the small digit drawn under the label (the full-band
    detector misses it: run 182022 raw_0002 read 'DAy' only); the text inside the click box below the label
    ('NEW', ...) is read too. The click box is the tile body under the label (the label itself takes no click,
    run 20261002_110614). Returns [{'day': int | None, 'text': str, 'box': native box}], left to right."""
    from . import extract as ex
    x0, y0, x1, y1 = TILE_BAND
    band = np.ascontiguousarray(n[y0:y1, x0:x1])
    try:
        lines = ex._ocr_boxes(band, 1)
    except Exception:
        return []
    eng = ex._ocr3_en or ex._get_ocr3()
    labels = [b for b in lines if DAY_LABEL_RE.match(b.text or "")]
    if labels:   # loop15: the open load box's title "Day 3" sits lower, in the centre -- it is not a tile
        top = min(b.y1 for b in labels)
        labels = [b for b in labels if b.y1 <= top + 8]
    out = []
    for lb in labels:
        lx1, ly1, lx2, ly2 = lb.x1 + x0, lb.y1 + y0, lb.x2 + x0, lb.y2 + y0
        cx = (lx1 + lx2) // 2
        m = DAY_LABEL_RE.match(lb.text)
        num = m.group(1) if m and m.group(1) else None
        if num is None and eng is not None:
            # the day number under the label
            dc = n[ly2 - 1:ly2 + 15, max(0, lx1 - 6):lx2 + 6]
            if dc.size:
                up = cv2.copyMakeBorder(cv2.resize(dc, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST),
                                        8, 8, 8, 8, cv2.BORDER_CONSTANT, value=0)
                try:
                    r = eng.recognize_txt([up])
                    d = re.sub(r"\D", "", r.txts[0] if r.txts else "")
                    num = d[:2] or None
                except Exception:
                    num = None
        box = (max(0, cx - 34), ly2 + 16, min(NATIVE_W, cx + 34), min(NATIVE_H, ly2 + 46))
        inner = " ".join(b.text for b in lines
                         if b is not lb and box[0] <= (b.x1 + b.x2) / 2 + x0 <= box[2]
                         and box[1] <= (b.y1 + b.y2) / 2 + y0 <= box[3] and b.text)
        txt = f"DAY {num}" if num else "DAY ?"
        out.append({"day": int(num) if num else None, "text": (txt + (" " + inner if inner else "")).strip(),
                    "box": box})
    out.sort(key=lambda t: t["box"][0])
    return out


def dialog_lines(n: np.ndarray) -> list[dict]:
    """Day-select screen below the tile row: every drawn text line, read by OCR (loop15). Clicking a day tile opens
    a box (day name, money, CONTINUE / CANCEL); run 20261003_195240 t4-59: the text detector never found CONTINUE
    (dim green on black), so lines are found as bright-pixel components and each is read by the recogniser.
    BACK is not offered (main-menu exit, as before). Returns [{'text': str, 'box': native box}]."""
    from . import extract as ex
    y0 = TILE_BAND[3]
    m = cv2.dilate((n[y0:].max(2) > 40).astype(np.uint8), np.ones((3, 9), np.uint8))
    k, _, st, _ = cv2.connectedComponentsWithStats(m)
    eng = ex._ocr3_en or ex._get_ocr3()
    if eng is None:
        return []
    out = []
    for i in range(1, k):
        x, y, w, h, _a = st[i]
        if w < 16 or not 5 <= h <= 40:
            continue
        box = (max(0, x - 2), y0 + y - 1, min(NATIVE_W, x + w + 2), min(NATIVE_H, y0 + y + h + 1))
        c = n[box[1]:box[3], box[0]:box[2]]
        up = cv2.copyMakeBorder(cv2.resize(c, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST),
                                8, 8, 8, 8, cv2.BORDER_CONSTANT, value=0)
        try:
            r = eng.recognize_txt([up])
            txt = (r.txts[0] if r.txts else "").strip()
        except Exception:
            txt = ""
        if len(txt) >= 3 and txt.upper() != "BACK":
            out.append({"text": txt, "box": box})
    out.sort(key=lambda t: (t["box"][1], t["box"][0]))
    return out


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


def _paper_colour(n: np.ndarray, b) -> int | None:
    """most common exact colour of the bright (max channel > 60) pixels in native box b, packed BGR"""
    c = n[b[1]:b[3], b[0]:b[2]].astype(np.int32)
    m = c.max(2) > 60
    if m.sum() < 20:
        return None
    k = (c[..., 0] << 16) | (c[..., 1] << 8) | c[..., 2]
    v, cnt = np.unique(k[m], return_counts=True)
    return int(v[cnt.argmax()])


def _merge_strip(boxes: list[list[int]], top: int, gap: int = 36, n: np.ndarray | None = None) -> list[list[int]]:
    """Paper peeking above the open tray is cut into pieces by the stamp knobs:
    merge pieces that touch the bar's top edge and are < gap apart."""
    # only pieces whose bottom IS the bar's top edge; a paper below the bar (its bottom far below `top`) is a
    # separate document (164732 tick 119: a slip below the bar was merged with the passport above it)
    def at_edge(b) -> bool:
        return top - 8 <= b[3] <= top + 2

    edge = sorted([b for b in boxes if at_edge(b)], key=lambda b: b[0])
    rest = [b for b in boxes if not at_edge(b)]
    out: list[list[int]] = []
    for b in edge:
        if out and b[0] - out[-1][2] <= gap:
            o = out[-1]
            out[-1] = [min(o[0], b[0]), min(o[1], b[1]), max(o[2], b[2]), max(o[3], b[3])]
        else:
            out.append(list(b))
    # a sheet straddling the bar (passport pushed half under the tray) shows above AND below it: rejoin the two
    # parts when they have the same left/right edges, or (knobs cut the top part) the same paper colour over
    # overlapping columns. A different paper below the bar stays separate.
    below = [b for b in rest if b[1] >= TRAY_BAR[3] - 4 and b[1] <= TRAY_BAR[3] + 2]
    for o in out:
        for b in below:
            if b not in rest:
                continue
            if (abs(b[0] - o[0]) <= 4 and abs(b[2] - o[2]) <= 4) or (
                    n is not None and min(o[2], b[2]) - max(o[0], b[0]) > 0.5 * (o[2] - o[0])
                    and _paper_colour(n, o) is not None and _paper_colour(n, o) == _paper_colour(n, b)):
                o[:] = [min(o[0], b[0]), o[1], max(o[2], b[2]), max(o[3], b[3])]
                rest.remove(b)
    return rest + out


def _split_by_paper(n: np.ndarray, b: list[int], drop_pages: bool = False,
                    siblings: list[list[int]] | None = None) -> list[list[int]]:
    """A component can be two overlapping documents (entry visa on the rulebook):
    the paper colours inside it (quantised to 24 levels) are separate sheets when
    one colour fills the component and another is a large sub-rectangle of it.
    siblings: the other sheets _sheets already found in the same component. A sub-rectangle lying inside one of
    them is that sheet's visible part over b (run 201459 t40-56: the entry ticket lying across the open passport):
    it is not a new document (it was a duplicate box of the ticket, and the passport became 3 pieces that TOD named
    entry_ticket), but b is still trimmed to its visible side next to it, so the ticket's lines are not read as
    b's (text is attributed to a document by its centre lying in the box)."""
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
    over = []      # sibling sheets' parts lying over b (trim b around them, never their own box)
    for s in subs:
        sa = (s[2] - s[0]) * (s[3] - s[1])
        if s in full or sa < 0.12 * area or sa > 0.7 * area:
            continue
        if drop_pages and abs(s[0] - b[0]) <= 4 and abs(s[2] - b[2]) <= 4:
            continue   # the other page of the same open booklet (a sheet _sheets already separated), not a paper
        if siblings and any(_inside4(s, o) >= 0.6 for o in siblings):
            over.append(s)   # a sibling sheet's part lying over b: that sheet is already its own document
            continue
        if all(_iou4(s, o) < 0.6 for o in out):
            out.append(s)
    if len(out) > 1 or over:
        # the sheet underneath must not keep the top sheet's area: text is attributed to a document by its centre
        # lying in the box, so a container box would read the top sheet's lines as its own (a citation slip
        # dropped on the passport made the passport 'say' M.O.A. CITATION). Keep the container's largest visible
        # side next to each top sheet.
        i = int(order[0])
        own = (key == vals[i]) & pm
        # (not for a sub-sheet sharing the container's left and right edges: that is the other page of the same
        # open passport, which Day 1 has always reported as container + data page)
        inner = [s for s in out[1:] + over if not (abs(s[0] - out[0][0]) <= 4 and abs(s[2] - out[0][2]) <= 4)]
        out[0] = _trim_around(out[0], inner, own, x1, y1)
    return out


def _trim_around(outer: list[int], inners: list[list[int]], own: np.ndarray, ox: int, oy: int) -> list[int]:
    """outer box minus each inner box -> the side rectangle (left/right/top/bottom of the inner one) holding the
    most of outer's own pixels (own: mask in the crop starting at (ox, oy)). Unchanged if no side is >= 8 px."""
    o = list(outer)
    for s in inners:
        ix1, iy1, ix2, iy2 = max(o[0], s[0]), max(o[1], s[1]), min(o[2], s[2]), min(o[3], s[3])
        if ix2 <= ix1 or iy2 <= iy1:
            continue
        sides = [[o[0], o[1], ix1, o[3]], [ix2, o[1], o[2], o[3]], [o[0], o[1], o[2], iy1], [o[0], iy2, o[2], o[3]]]
        best, best_n = None, 0
        for c in sides:
            if c[2] - c[0] < 8 or c[3] - c[1] < 8:
                continue
            k = int(own[c[1] - oy:c[3] - oy, c[0] - ox:c[2] - ox].sum())
            if k > best_n:
                best, best_n = c, k
        if best is not None:
            o = best
    return o


SHEET_MIN_PX = 250      # a flat paper colour covering fewer native px than this is ink, not a sheet
SHEET_CLOSE = 7         # closing kernel that fills a sheet's printed text (pixel font lines are 1-3 px apart)
SHEET_FILL = 0.45       # a sheet's colour fills at least this much of its own bbox (after closing)


def _sheets(n: np.ndarray, b: list[int], valid: np.ndarray) -> list[list[int]]:
    """Split one desk component into the separate sheets lying SIDE BY SIDE / overlapping at the edges.
    Every paper in the game is printed on one flat colour (Arstotzkan passport cream (237,224,216) RGB, citation
    slip pink-white (243,215,230), Pink Vice flyer purple (60,38,92)), so each exact colour that covers a solid
    region is a sheet candidate. A region lying mostly inside an already accepted sheet is that sheet's ink /
    photo / a sheet on top (left to _split_by_paper). Stacked regions with the same left and right edges are the
    two pages of one open booklet and are rejoined. valid: the desk mask (paper pixels) for the whole frame."""
    x1, y1, x2, y2 = b
    c = n[y1:y2, x1:x2]
    vm = valid[y1:y2, x1:x2].astype(bool)
    tot = int(vm.sum())
    if tot < 2 * SHEET_MIN_PX:
        return [b]
    c32 = c.astype(np.int32)
    key = (c32[..., 0] << 16) | (c32[..., 1] << 8) | c32[..., 2]
    vals, cnt = np.unique(key[vm], return_counts=True)
    regions = []   # (px, colour key, box)
    k = np.ones((SHEET_CLOSE, SHEET_CLOSE), np.uint8)
    for i in np.argsort(-cnt)[:6]:
        if cnt[i] < max(SHEET_MIN_PX, 0.05 * tot):
            break
        mc = ((key == vals[i]) & vm).astype(np.uint8)
        mcl = cv2.morphologyEx(mc, cv2.MORPH_CLOSE, k)
        nl, lab, st, _ = cv2.connectedComponentsWithStats(mcl, connectivity=8)
        for j in range(1, nl):
            rx, ry, rw, rh, _a = st[j]
            px = int(mc[lab == j].sum())
            if px < SHEET_MIN_PX or rw < 12 or rh < 10 or int(st[j][4]) < SHEET_FILL * rw * rh:
                continue
            regions.append((px, int(vals[i]), [int(x1 + rx), int(y1 + ry), int(x1 + rx + rw), int(y1 + ry + rh)]))
    regions.sort(key=lambda r: -r[0])
    sheets: list[list] = []   # [colour, box]
    for px, col, rb in regions:
        if any(_inside4(rb, s[1]) >= 0.6 for s in sheets):
            continue   # ink, a photo, or a sheet lying on top of an accepted one (_split_by_paper's case)
        same = [s for s in sheets if s[0] == col]
        if same:       # the same paper showing on both sides of a sheet lying across it
            s = same[0]
            s[1] = [min(s[1][0], rb[0]), min(s[1][1], rb[1]), max(s[1][2], rb[2]), max(s[1][3], rb[3])]
            continue
        sheets.append([col, rb])
    # two pages of one open booklet: same left/right edges, touching vertically
    merged = True
    while merged:
        merged = False
        for a in sheets:
            for o in sheets:
                if a is o:
                    continue
                if abs(a[1][0] - o[1][0]) <= 3 and abs(a[1][2] - o[1][2]) <= 3 and -3 <= o[1][1] - a[1][3] <= 3:
                    a[1] = [min(a[1][0], o[1][0]), a[1][1], max(a[1][2], o[1][2]), o[1][3]]
                    sheets.remove(o)
                    merged = True
                    break
            if merged:
                break
    if len(sheets) < 2:
        return [b]
    return [s[1] for s in sheets]


def _inside4(a, b) -> float:
    """fraction of box a lying inside box b"""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    aa = (a[2] - a[0]) * (a[3] - a[1])
    return ix * iy / aa if aa else 0.0


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
    ib = BY_NAME["inspect_toggle"].box   # Day 2+ red INSPECT button in the desk's corner is not a document
    if int(_red(_crop(n, ib)).sum()) > 40:
        m[ib[1] - y0:ib[3] - y0, ib[0] - x0:ib[2] - x0] = False
    m = cv2.morphologyEx(m.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    valid = np.zeros(n.shape[:2], bool)
    valid[y0:y1_, x0:x1_] = m > 0
    comps = [[a + x0, b + y0, c + x0, e + y0] for a, b, c, e in _components(m, DOC_MIN_AREA)]
    if is_tray_open:
        comps = _merge_strip(comps, TRAY_BAR[1], n=n)
    # a knob-cut fragment lying inside another document's box is part of it
    comps = [b for i, b in enumerate(comps) if not any(
        j != i and o[0] <= b[0] and o[1] <= b[1] and b[2] <= o[2] + 1 and b[3] <= o[3]
        and (o[2] - o[0]) * (o[3] - o[1]) > (b[2] - b[0]) * (b[3] - b[1]) for j, o in enumerate(comps))]
    for b in comps:
        shs = _sheets(n, b, valid)
        for s in (s2 for sh in shs for s2 in _split_by_paper(n, sh, drop_pages=len(shs) > 1,
                                                             siblings=[o for o in shs if o is not sh])):
            s = [int(v) for v in s]
            docs.append({"where": "desk", "box": s, "under_tray": is_tray_open and s[3] >= TRAY_BAR[1] - 2
                         and s[1] >= DESK[1] and s[0] >= TRAY_BAR[0] - 2})
    # counter: difference vs the empty reference counter
    ref = _A()["ref_booth"]
    cx1, cy1, cx2, cy2 = COUNTER
    diff = np.abs(_crop(n, COUNTER).astype(np.int16) - _crop(ref, COUNTER).astype(np.int16)).max(2) > 30
    for b in _components(diff, 60):
        docs.append({"where": "counter", "box": [b[0] + cx1, b[1] + cy1, b[2] + cx1, b[3] + cy1]})
    return docs


# --------------------------------------------------------------------------
# where to put the open passport on the desk (drop-target derivation, native px)
# --------------------------------------------------------------------------

# open passport = visa page (top) over data page (bottom: name, DOB, ISS., EXP., country, number). Fully visible
# open passports in runs 015649-150111 measure 130 x 162 (91 boxes, e.g. [204,115,334,277]); the OCR lists
# 'ENTRY VISA' first and the data lines below it.
OPEN_PASSPORT = (130, 162)
PASSPORT_DATA_FRAC = 0.5     # lower half of the open passport = the data page
DESK_MARGIN = 4              # keep the passport this far inside the desk / frame edge
EDGE_TOL = 2                 # a visible box this close to a desk/frame edge is clipped there
# counter -> desk: the closed passport opens CENTRED on the drop point (117 drops in 2026-10-03 runs: grab at the
# closed passport's centre, drop (268,267) -> open box x1 = drop-65, y1 = drop-81). Desk -> desk: the paper keeps
# its offset to the cursor (box moves by drop - grab; 95 re-drags, residual 0-1 px).


def desk_obstacles(docs: list[dict], is_tray_open: bool, inspect_button: bool = False,
                   exclude: list | None = None) -> list[tuple[str, list[int], float]]:
    """(name, native box, weight) of everything the open passport must not lie under or on: the open stamp bar,
    the stamp knobs and the open-tray tab (the tray draws OVER papers); with the tray closed the bar's area at a
    low weight (opening the tray would cover it) and the closed tray tab; the inspect button; every other paper
    box on the desk (excluding `exclude`, the passport itself)."""
    out: list[tuple[str, list[int], float]] = []
    bx1, by1, bx2, by2 = TRAY_BAR
    if is_tray_open:
        out.append(("tray_bar", list(TRAY_BAR), 1.0))
        out += [("stamp_knob", [kx1, DESK[1], kx2, by1], 1.0) for kx1, kx2 in KNOB_X]
        out.append(("tray_tab_open", list(BY_NAME["tray_tab_open"].box), 1.0))
    else:
        out.append(("tray_bar_area", list(TRAY_BAR), 0.25))
        out.append(("tray_tab", list(BY_NAME["tray_tab"].box), 1.0))
    if inspect_button:
        out.append(("inspect_toggle", list(BY_NAME["inspect_toggle"].box), 1.0))
    ex_ = [list(e) for e in (exclude or [])]
    for d in docs:
        if d.get("where") != "desk":
            continue
        b = list(d.get("native") or d["box"])
        if any(_iou4(b, e) > 0.5 or (e[0] <= b[0] and e[1] <= b[1] and b[2] <= e[2] and b[3] <= e[3]) for e in ex_):
            continue
        out.append(("paper", b, 1.0))
    return out


def _ov(a, b) -> int:
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def passport_obstruction(box, obstacles: list) -> dict:
    """Geometric check of one (full, native) open-passport box: overlap px per obstacle name, the part off the
    desk / frame, and the hidden fraction of the data page (lower half). Used by the derivation and the replay."""
    x1, y1, x2, y2 = box
    data = [x1, int(y1 + (1 - PASSPORT_DATA_FRAC) * (y2 - y1)), x2, y2]
    inside = _ov(box, DESK)
    res = {"off_desk": (x2 - x1) * (y2 - y1) - inside, "data_hidden": 0, "by": {}}
    for name, b, w in obstacles:
        if w < 0.5:
            continue   # the closed tray's bar area hides nothing yet
        o = _ov(box, b)
        if o:
            res["by"][name] = res["by"].get(name, 0) + o
            res["data_hidden"] += _ov(data, b)
    dpx = (data[2] - data[0]) * (data[3] - data[1])
    res["data_hidden"] = min(dpx, res["data_hidden"] + dpx - _ov(data, DESK))   # covered + off the desk/frame
    res["data_hidden_frac"] = round(res["data_hidden"] / dpx, 3) if dpx else 1.0
    res["clear"] = res["off_desk"] == 0 and not res["by"]
    return res


def open_passport_size(vis_box=None) -> tuple[int, int]:
    """Open-passport size from the frame: the width of a visible open passport that is not clipped left/right
    (its height is clipped by the frame bottom / the tray far more often), height by the fixture's aspect."""
    w0, h0 = OPEN_PASSPORT
    if vis_box is not None:
        x1, _, x2, _ = vis_box
        w = x2 - x1
        if x1 > DESK[0] + EDGE_TOL and x2 < NATIVE_W - EDGE_TOL and 0.8 * w0 <= w <= 1.2 * w0:
            return w, int(round(w * h0 / w0))
    return w0, h0


def full_passport_box(vis, size, is_tray_open: bool) -> list[int]:
    """The whole open passport behind a visible (possibly clipped) box: clipped at the frame bottom -> the top
    edge is real; clipped at the desk top / cut by the open stamp bar from above -> the bottom edge is real."""
    x1, y1, x2, y2 = vis
    w, h = size
    if x1 <= DESK[0] + EDGE_TOL and x2 - x1 < w:
        x1 = x2 - w
    x2 = x1 + w
    cut_top = y1 <= DESK[1] + EDGE_TOL or (is_tray_open and abs(y1 - TRAY_BAR[3]) <= 3 and x2 > TRAY_BAR[0])
    if y2 - y1 >= h - EDGE_TOL or not cut_top:
        return [x1, y1, x2, y1 + h]
    return [x1, y2 - h, x2, y2]


def full_sheet_box(vis, size, is_tray_open: bool, others: list | None = None) -> list[int]:
    """full_passport_box for a visible box that another paper may trim (5d1669c: the entry ticket lying across the
    open passport splits it, the passport box is only its visible strip). An edge of `vis` that touches / lies under
    another paper (`others`, native boxes) is not a real sheet edge: the sheet extends past it -- upward when the
    top is covered (or cut by the stamp bar), downward when the bottom is, left/right likewise. Uncovered edges are
    real. Without overlap this is full_passport_box."""
    x1, y1, x2, y2 = [int(v) for v in vis]
    w, h = int(size[0]), int(size[1])
    tol = EDGE_TOL + 2

    def covers(o, edge):   # another paper reaches across this edge of vis
        ox1, oy1, ox2, oy2 = o
        if edge == "top":
            return oy1 <= y1 + tol and oy2 >= y1 - tol and ox1 < x2 and ox2 > x1
        if edge == "bottom":
            return oy1 <= y2 + tol and oy2 >= y2 - tol and ox1 < x2 and ox2 > x1
        if edge == "left":
            return ox1 <= x1 + tol and ox2 >= x1 - tol and oy1 < y2 and oy2 > y1
        return ox1 <= x2 + tol and ox2 >= x2 - tol and oy1 < y2 and oy2 > y1
    others = [list(o) for o in (others or []) if _ov(o, vis) > 0 or any(covers(o, e) for e in
                                                                        ("top", "bottom", "left", "right"))]
    if not others:
        return full_passport_box(vis, size, is_tray_open)
    cov = {e: any(covers(o, e) for o in others) for e in ("top", "bottom", "left", "right")}
    cut_top = (cov["top"] or y1 <= DESK[1] + EDGE_TOL
               or (is_tray_open and abs(y1 - TRAY_BAR[3]) <= 3 and x2 > TRAY_BAR[0]))
    if x2 - x1 < w - EDGE_TOL and (cov["left"] or x1 <= DESK[0] + EDGE_TOL) and not cov["right"]:
        fx1 = x2 - w
    else:
        fx1 = x1
    if y2 - y1 >= h - EDGE_TOL:
        fy1 = y1
    elif cut_top and not cov["bottom"]:
        fy1 = y2 - h
    else:
        fy1 = y1
    return [fx1, fy1, fx1 + w, fy1 + h]


def passport_grab_point(vis, others: list | None = None, inset: int = 6) -> tuple[int, int]:
    """A point on the passport itself (native): inside its visible box `vis`, `inset` px in, outside every other
    paper's box (`others`, e.g. the entry ticket lying across it) -- the one nearest the box centre. The plain box
    centre when nothing overlaps (or no free point exists)."""
    x1, y1, x2, y2 = [int(v) for v in vis]
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    obs = [o for o in (others or []) if _ov(o, vis) > 0]
    if not obs:
        return cx, cy
    best, bd = None, None
    for y in range(y1 + inset, max(y1 + inset, y2 - inset) + 1, 2):
        for x in range(x1 + inset, max(x1 + inset, x2 - inset) + 1, 2):
            if any(o[0] - inset <= x <= o[2] + inset and o[1] - inset <= y <= o[3] + inset for o in obs):
                continue
            d = (x - cx) ** 2 + (y - cy) ** 2
            if bd is None or d < bd:
                best, bd = (x, y), d
    return best or (cx, cy)


def head_footprint(side: str, stamp_box=None) -> list[int]:
    """Where the stamp head inks (native): STRIP_X width centred on the detected stamp box (native, x-centre), the
    strip rows STRIP_Y; the fixture strip when no stamp box was detected."""
    sx1, sx2 = STRIP_X[side]
    if stamp_box is not None:
        c, hw = (stamp_box[0] + stamp_box[2]) / 2, (sx2 - sx1) / 2
        sx1, sx2 = int(round(c - hw)), int(round(c + hw))
    return [sx1, STRIP_Y[0], sx2, STRIP_Y[1]]


def visa_page(full) -> list[int]:
    """Upper (visa) page of an open passport box."""
    x1, y1, x2, y2 = full
    return [x1, y1, x2, int(round(y1 + (y2 - y1) * (1 - PASSPORT_DATA_FRAC)))]


def contains(outer, inner) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def clear_desk_spot(docs: list[dict], is_tray_open: bool, size=OPEN_PASSPORT, inspect_button: bool = False,
                    exclude: list | None = None) -> dict:
    """Where the open passport (size w x h, native) lies fully on the desk with the least covered: every
    position inside DESK (DESK_MARGIN in) is scored by the obstacle area under it (desk_obstacles weights; the
    data page -- lower half -- counts 3x), ties go to the position whose centre is farthest from any obstacle
    (the largest clear region). Returns {'box', 'center', 'cost', 'check' (passport_obstruction)}."""
    w, h = int(size[0]), int(size[1])
    obst = desk_obstacles(docs, is_tray_open, inspect_button, exclude)
    cost = np.zeros((NATIVE_H, NATIVE_W), np.float32)
    for _, (ox1, oy1, ox2, oy2), wt in obst:
        sl = cost[max(0, oy1):max(0, oy2), max(0, ox1):max(0, ox2)]
        np.maximum(sl, wt, out=sl)
    ii = cv2.integral(cost)   # (H+1, W+1)
    m = DESK_MARGIN
    xs = np.arange(DESK[0] + m, max(DESK[0] + m, DESK[2] - m - w) + 1)
    ys = np.arange(DESK[1] + m, max(DESK[1] + m, NATIVE_H - m - h) + 1)
    X, Y = np.meshgrid(xs, ys)
    X2, Y2 = np.minimum(X + w, NATIVE_W), np.minimum(Y + h, NATIVE_H)
    Ym = Y + int((1 - PASSPORT_DATA_FRAC) * h)

    def S(a, b, c, d):
        return ii[d, c] - ii[b, c] - ii[d, a] + ii[b, a]
    total = S(X, Y, X2, Y2) + 2.0 * S(X, Ym, X2, Y2)
    free = (cost < 0.5).astype(np.uint8)
    free[:DESK[1], :] = 0
    free[:, :DESK[0]] = 0
    dist = cv2.distanceTransform(free, cv2.DIST_L2, 3)
    cx, cy = np.minimum(X + w // 2, NATIVE_W - 1), np.minimum(Y + h // 2, NATIVE_H - 1)
    best = total.min()
    score = np.where(total <= best + 1e-3, dist[cy, cx], -1.0)
    i, j = np.unravel_index(int(np.argmax(score)), score.shape)
    x1, y1 = int(X[i, j]), int(Y[i, j])
    box = [x1, y1, x1 + w, y1 + h]
    return {"box": box, "center": (x1 + w // 2, y1 + h // 2), "cost": round(float(best), 1),
            "check": passport_obstruction(box, obst)}


STOW_SIDE_WEIGHT = 0.2   # an inspector's paper (bulletin / rulebook) under the stowed flyer: avoided, not forbidden


def stow_spot(size, keep_clear: list, others: list, inspect_button: bool = False) -> dict:
    """loop23 (run 023151 t41-82): where a flyer / citation slip is put away -- a clear patch of the DESK surface,
    derived per frame: never the counter shelf (where the entrant's passport and ticket arrive), never the stamp
    tray bar / landing strips / knob row (the bar covers them once the tray opens), the tray tabs, the inspect button
    or `keep_clear` ([(native box, weight)]: the entrant's papers high, the planned passport spot 1.0); `others` (inspector's
    papers) at a low weight. The paper (size w x h, native) lies fully inside the desk; among the cheapest positions
    the far-left / top-left one wins. Returns {'box', 'center', 'cost'}."""
    w, h = int(size[0]), int(size[1])
    obst = [([TRAY_BAR[0], DESK[1], TRAY_BAR[2], TRAY_BAR[3]], 1.0),   # bar + strips + knob row, open or closed
            (list(BY_NAME["tray_tab"].box), 1.0), (list(BY_NAME["tray_tab_open"].box), 1.0)]
    if inspect_button:
        obst.append((list(BY_NAME["inspect_toggle"].box), 1.0))
    obst += [(list(b), wt) for b, wt in keep_clear or []] + [(list(b), STOW_SIDE_WEIGHT) for b in others or []]
    cost = np.zeros((NATIVE_H, NATIVE_W), np.float32)
    for (ox1, oy1, ox2, oy2), wt in obst:
        sl = cost[max(0, int(oy1)):max(0, int(oy2)), max(0, int(ox1)):max(0, int(ox2))]
        np.maximum(sl, wt, out=sl)
    ii = cv2.integral(cost)
    m = DESK_MARGIN
    w, h = min(w, DESK[2] - DESK[0] - 2 * m), min(h, NATIVE_H - DESK[1] - 2 * m)
    xs = np.arange(DESK[0] + m, DESK[2] - m - w + 1)
    ys = np.arange(DESK[1] + m, NATIVE_H - m - h + 1)
    X, Y = np.meshgrid(xs, ys)
    total = ii[Y + h, X + w] - ii[Y, X + w] - ii[Y + h, X] + ii[Y, X]
    best = float(total.min())
    pref = np.where(total <= best + 1e-3, -(X + 0.5 * Y).astype(np.float32), -1e9)
    i, j = np.unravel_index(int(np.argmax(pref)), pref.shape)
    x1, y1 = int(X[i, j]), int(Y[i, j])
    return {"box": [x1, y1, x1 + w, y1 + h], "center": (x1 + w // 2, y1 + h // 2), "cost": round(best, 1)}


def passport_drop_point(spot: dict, src: dict | None, size, is_tray_open: bool, others: list | None = None,
                        grab=None) -> tuple[int, int]:
    """Cursor end point (native) that puts the passport onto `spot`. src = the passport's paper dict
    ({'where', 'box'/'native'}) or None. From the counter (closed passport) it opens centred on the cursor;
    a paper dragged on the desk keeps its offset to the grab point (`grab`, native; default the centre of its
    visible box). `others`: other desk papers (a box trimmed by one of them is anchored by full_sheet_box)."""
    cx, cy = spot["center"]
    if src is not None and src.get("where") == "desk":
        vis = list(src.get("native") or src["box"])
        full = full_sheet_box(vis, size, is_tray_open, others)
        gx, gy = grab if grab is not None else ((vis[0] + vis[2]) / 2, (vis[1] + vis[3]) / 2)
        cx += gx - (full[0] + full[2]) / 2
        cy += gy - (full[1] + full[3]) / 2
    # the cursor must end on the desk (papers released over the counter close and fall back onto it)
    m = 3 * DESK_MARGIN
    return (int(round(min(max(cx, DESK[0] + m), DESK[2] - m))), int(round(min(max(cy, DESK[1] + m), NATIVE_H - m))))


_DOC_DESC = {
    "desk":"document on the desk -- drag it (onto a stamp landing strip to stamp it, onto the person at the "
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
    docs: list[dict] = []
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
        docs = find_documents(n, flags["tray_open"])
        flags["counter_empty"] = not any(d["where"] == "counter" for d in docs)
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
    if screen == "day_select":
        # loop14: every drawn day tile from OCR, with its text, replaces the fixed day1..3 boxes (loop13: only
        # DAY 1 was ever offered; the fixed tile2/3 boxes were guesses without captions)
        tiles = day_tiles(n)
        flags["day_tiles"] = [t["text"] for t in tiles]
        if tiles:
            out = [b for b in out if not getattr(b, "name", "").startswith("day") or not b.name.endswith("_tile")]
            for i, t in enumerate(tiles):
                x1, y1, x2, y2 = scale_box(t["box"], W, H)
                out.append(LBox(x1, y1, x2, y2, t["text"], "object", 1.0, caption="day tile",
                                name=f"day_tile_{t['day'] or i}", affordance="click"))
        lines = dialog_lines(n)
        flags["dialog_lines"] = [t["text"] for t in lines]
        for i, t in enumerate(lines):
            x1, y1, x2, y2 = scale_box(t["box"], W, H)
            out.append(LBox(x1, y1, x2, y2, t["text"], "text", 1.0, caption="text line under the day tiles",
                            name=f"dialog_{i}", affordance="click"))
    if screen == "booth":
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
# inspect-mode text the game prints outside the desk / counter (the bottom-bar "HIGHLIGHT DISCREPANCIES", the
# interrogate prompt below the counter): vision's OCR box for it is kept wherever it lies on the booth
PROMPT_TEXT_RE = re.compile(r"INTERROG|HIGHL[I1l]GHT|D[I1l]SCREPAN|NO\s*DOCUMENTS", re.I)


def _prompt_text(v: Box) -> bool:
    return v.kind == "text" and bool(PROMPT_TEXT_RE.search(v.text or ""))


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
    if screen in ("title", "day_select") or any(getattr(f, "name", "") in _MENU_NAMES
                                                or getattr(f, "name", "").startswith("day_tile_") for f in fixed):
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

        keep = [v for v in keep if (doc_area(v) or _prompt_text(v))
                and not inside(v, label) >= 0.6
                and not (tray and inside(v, bar) >= 0.8)]
    vdocs = [v for v in keep if any(iou(v, d) > 0.1 for d in docs)]
    # loop23 (run 030640 t47-62): vision boxed the desk papers but not the ticket on the counter shelf -> the static
    # counter doc was dropped with the rest and the paper TOD's B3 names had no mark. A static counter-shelf doc that
    # no vision box overlaps is kept on its own
    lone = [d for d in docs if getattr(d, "name", "").startswith("doc_counter")
            and not any(iou(v, d) > 0.1 or inside(v, d) >= 0.5 for v in keep)]
    out = fixed + keep + (lone if vdocs else docs)
    if frame_wh is not None and screen == "booth" and LAST.get("docs"):
        refine_docs_by_text(LAST["docs"], out, *frame_wh)
    return out


def refine_docs_by_text(docs: list[dict], boxes: list[Box], W: int, H: int) -> None:
    """OCR safety net over the colour split, IN PLACE on find_documents' list (LAST['docs'], which the loop
    passes on as sinfo['docs'] and reads each document's text from: every text box centred in the doc box).
    A desk document whose lines are of two paper kinds (extract.PAPER_LINE_RES: citation / flyer / passport
    fields) still covers part of another paper (a mostly hidden flyer band across the passport's top). The doc
    keeps its majority kind (ties: passport); its box is trimmed until the other kind's lines are outside it
    (never cutting off one of its own lines), and those lines become a doc of their own ('partial': True) --
    a partly visible slip / flyer still gets a box with its visible text."""
    from . import extract as ex
    sx, sy = W / NATIVE_W, H / NATIVE_H
    lines = []
    for b in boxes:
        k = ex._line_kind(b.text or "") if (b.text or "").strip() else None
        if k:
            lines.append((k, b.center[0] / sx, b.center[1] / sy, [b.x1 / sx, b.y1 / sy, b.x2 / sx, b.y2 / sy]))
    if not lines:
        return
    pri = {"passport": 0, "citation": 1, "flyer": 2}
    for d in list(docs):
        if d.get("where") != "desk":
            continue
        bx = d["box"]
        mine = [ln for ln in lines if bx[0] <= ln[1] <= bx[2] and bx[1] <= ln[2] <= bx[3]]
        kinds: dict[str, list] = {}
        for ln in mine:
            kinds.setdefault(ln[0], []).append(ln)
        if len(kinds) < 2:
            continue
        keep = min(kinds, key=lambda k: (-len(kinds[k]), pri.get(k, 9)))
        own = kinds[keep]
        o = list(bx)
        for k, ls in kinds.items():
            if k == keep:
                continue
            for _, cx, cy, lb in ls:
                if not (o[0] <= cx <= o[2] and o[1] <= cy <= o[3]):
                    continue
                # candidate cuts: put the foreign line's box outside on one side, keep every own line centre in
                cuts = [[o[0], max(o[1], int(np.ceil(lb[3]))), o[2], o[3]], [o[0], o[1], o[2], min(o[3], int(lb[1]))],
                        [max(o[0], int(np.ceil(lb[2]))), o[1], o[2], o[3]], [o[0], o[1], min(o[2], int(lb[0])), o[3]]]
                cuts = [c for c in cuts if c[2] - c[0] >= 8 and c[3] - c[1] >= 8
                        and all(c[0] <= ox <= c[2] and c[1] <= oy <= c[3] for _, ox, oy, _ in own)]
                if cuts:
                    o = max(cuts, key=lambda c: (c[2] - c[0]) * (c[3] - c[1]))
            # the other paper's visible lines -> its own (partial) document box
            gb = [int(min(ln[3][0] for ln in ls)) - 2, int(min(ln[3][1] for ln in ls)) - 2,
                  int(np.ceil(max(ln[3][2] for ln in ls))) + 2, int(np.ceil(max(ln[3][3] for ln in ls))) + 2]
            if not any(dd is not d and dd.get("where") == "desk"
                       and all(dd["box"][0] <= ln[1] <= dd["box"][2] and dd["box"][1] <= ln[2] <= dd["box"][3]
                               for ln in ls) for dd in docs):
                under = bool(LAST.get("flags", {}).get("tray_open")) and gb[1] < TRAY_BAR[3] and gb[0] >= TRAY_BAR[0] - 2
                docs.append({"where": "desk", "box": gb, "under_tray": under, "partial": True})
        d["box"] = [int(v) for v in o]
