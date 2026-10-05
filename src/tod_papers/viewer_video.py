"""Video layout of the behind-the-scenes panel (docs/viewer.md, `--layout video`).

Same content as the debug layout (viewer.Panel) -- nothing dropped -- re-flowed for the sidebar beside the
game (default 1176 x 1280, or a full 2160-high column) in type that stays legible when the recording is zoomed
out to the whole 3456-wide display (body text >= 22 px physical):

    header          run, tick
    SoM frame       "what TOD was shown - request 2": the still image TOD received, with the amber action marker
      | last 8      (compact layout) the last 8 actions as a list beside the frame, newest at the bottom
    what TOD sees   every request-1 answer with its p, two columns, changed-since-last-tick highlighted
    papers          paper identities by place (request 1b)
    situation       the WHAT APPLIES NOW sentence(s) sent with request 2, plus today's rule
    verdict         bar with all three probabilities (or the stored verdict)
    TOD's pick      action probabilities, top-3 options with bars / p / object description, drop target,
                    executed action with coordinates, input-convention / excluded / guard notes (small)
    last 8          (tall layout) as a chip strip
    footer          ground truth from game memory (not shown to TOD), latency breakdown

Every region has a fixed slot (sized for the longest content seen in real runs), so nothing jumps between ticks.

Motion: `Scene` is everything one tick shows; `VideoPanel.draw(cur, prev, t)` renders it `t` seconds after the
tick arrived: the frame crossfades from the previous tick (TRANS_S), the action marker grows in, changed facts
flash amber and slide up, a changed situation sentence fades in, verdict segments and pick bars tween from the
previous tick's probabilities, the executed line fades in, and the action list scrolls by one. After ANIM_END
everything is static (the final frame is cached), so a held tick costs nothing to show.
Pure rendering on files the loop already wrote; nothing here talks to the loop or the game.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image, ImageDraw

from . import viewer as V

# ---------------------------------------------------------------- theme (the debug palette, slightly deeper)
BG = (16, 18, 22)
CARD = (30, 33, 40)
CHANGED_BG = V.CHANGED_BG
FLASH_BG = (120, 96, 30)
FG = V.FG
DIM = V.DIM
FAINT = (92, 98, 110)
ACCENT = V.ACCENT
AMBER = V.AMBER
GREEN = V.GREEN
RED = V.RED
GREY = (120, 126, 140)

TRANS_S = 0.25    # frame crossfade
ANIM_END = 0.45   # every tween done by then (export hold is >= this)

# default geometry: 3456x2160 display, borderless game client 2280x1280 at the left -> 1176 px to its right
GAME_W, GAME_H = 2280, 1280
TILE_W, TILE_H = 3456 - GAME_W, 648      # one 16:9 tile = what the Cap zoom (amount 2.939) fills the screen with
DEFAULT_W, DEFAULT_H = TILE_W, 3 * TILE_H  # default: three stacked tiles, 1176 x 1944 at (2280, 0)
TALL_H = 1700     # at or above this height (non-tile): full-width frame, bigger type, action strip at the bottom


def tile_rects(x0: int = GAME_W, y0: int = 0, w: int = TILE_W, h: int = TILE_H) -> list[tuple[int, int, int, int]]:
    """Screen rects (x, y, w, h) of the three tiles of the default column (the Cap zoom plan pans between them)."""
    return [(x0, y0 + i * h, w, h) for i in range(3)]

VERDICT_ITEMS = [("approved", "APPROVED", GREEN), ("denied", "DENIED", RED),
                 ("cannot_decide_yet", "cannot decide yet", GREY)]


def _ease(x: float) -> float:          # ease-out cubic on [0, 1]
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3


def _phase(t: float, a: float, b: float) -> float:
    return _ease((t - a) / (b - a)) if b > a else 1.0


def _mix(c0, c1, k: float):
    k = min(max(k, 0.0), 1.0)
    return tuple(int(round(a + (b - a) * k)) for a, b in zip(c0, c1))


# ---------------------------------------------------------------- scene
@dataclass
class Scene:
    tick: int
    run: str
    rec: dict
    prev_rec: dict | None                       # previous tick on disk (change highlight, tick period) - as debug
    history: list[dict] = field(default_factory=list)   # last 8 records, oldest first, this tick last
    som: Image.Image | None = None              # resized SoM frame (RGB)
    raw: np.ndarray | None = None               # BGR raw frame (export composite only)
    act_pts: list[tuple[float, float]] = field(default_factory=list)   # executed click / drag ends, 0..1 of frame


def action_points(rec: dict) -> list[tuple[float, float]]:
    """Where the executed click / drag happened, as fractions of the frame (marker on the SoM frame)."""
    import re
    ex = str(rec.get("executed") or "")
    if ex.startswith("vetoed"):
        return []
    fs = rec.get("frame_shape") or [1280, 2280]
    h, w = float(fs[0]) or 1280.0, float(fs[1]) or 2280.0
    return [(int(x) / w, int(y) / h) for x, y in re.findall(r"\((\d+),\s*(\d+)\)", ex)][:2]


def build_scene(reader: V.RunReader, tick: int, som_size: tuple[int, int] | None, want_raw: bool = False,
                n_hist: int = 8) -> Scene | None:
    rec = reader.rec(tick)
    if rec is None:
        return None
    som = reader.som(tick)
    som_img = None
    if som is not None and som_size:
        small = cv2.resize(som, som_size, interpolation=cv2.INTER_AREA)
        som_img = Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
    raw = None
    if want_raw:
        p = os.path.join(reader.run_dir, f"raw_{tick:04d}.png")
        raw = cv2.imread(p) if os.path.exists(p) else som
    ticks = [t for t in V.list_ticks(reader.run_dir) if t <= tick][-n_hist:]
    hist = [rec if t == tick else reader.rec(t) for t in ticks]
    hist = [h for h in hist if h is not None]
    prev_rec = reader.rec(ticks[-2]) if len(ticks) >= 2 else None
    return Scene(tick=tick, run=os.path.basename(reader.run_dir.rstrip("\\/")), rec=rec, prev_rec=prev_rec,
                 history=hist, som=som_img, raw=raw, act_pts=action_points(rec))


# ---------------------------------------------------------------- rendering
class VideoPanel:
    def __init__(self, width: int = DEFAULT_W, height: int = DEFAULT_H):
        self.W, self.H = width, height
        # three-tile column: height = 3 tiles of TILE_W x TILE_H (scaled with the width), e.g. 1176 x 1944
        self.tiles = abs(height - 3 * round(width * TILE_H / TILE_W)) <= 3
        self.tall = height >= TALL_H and not self.tiles
        k = width / DEFAULT_W
        f = k * (1.14 if self.tall else 1.0)
        self.s, self.f = k, f
        F = V._font
        self.f_title = F("seguisb.ttf", int(26 * f))
        self.f_head = F("segoeuib.ttf", int(23 * f))
        self.f_body = F("segoeui.ttf", int(23 * f))
        self.f_bold = F("segoeuib.ttf", int(23 * f))
        self.f_small = F("segoeui.ttf", int(22 * f))
        self.f_mono = F("consola.ttf", int(22 * f))
        self.f_note = F("segoeui.ttf", int((22 if self.tiles else 21) * f))
        self.pad = int(16 * k)
        self._layout()
        self._final: tuple | None = None

    def _u(self, v: float) -> int:          # vertical unit, scales with the type
        return int(round(v * self.f))

    # -- geometry (fixed per panel size, so nothing jumps between ticks)
    def _layout(self):
        if self.tiles:
            return self._layout_tiles()
        u, W, H, pad = self._u, self.W, self.H, self.pad
        self.head_h = u(30)
        self.row_h = u(29)                   # fact rows
        self.line_b = u(29)                  # body line
        self.line_s = u(27)                  # small line
        self.cand_h = u(31)
        self.gap = u(8)
        self.sit_lines = 4
        self.papers_lines = 2 if self.tall else 1
        self.exec_lines = 2 if self.tall else 1
        self.notes_lines = 2 if self.tall else 1
        self.note_h = u(25)
        y = u(8)
        self.y_title = y
        y += u(38)
        # the text below the frame needs this much (fixed slots)
        below = (self.head_h + 7 * self.row_h + self.papers_lines * self.line_s + self.gap      # facts + papers
                 + self.head_h + self.sit_lines * self.line_b + 2 * self.line_s + self.gap      # situation + rule
                 + self.head_h + 3 * self.cand_h + self.line_s + self.line_s                    # pick: cands, 2nd line
                 + self.exec_lines * self.line_s + self.notes_lines * self.note_h + self.gap    # exec, notes
                 + 2 * self.line_s + u(10))                                                     # footer
        if self.tall:
            below += self.head_h + u(32) + self.line_s + self.gap                              # verdict bar
            below += self.head_h + u(84) + self.gap                                            # chip strip
        avail = H - y - below - self.head_h - self.gap - u(10)
        if self.tall:
            fw = W - 2 * pad
        else:
            fw = int(W * 0.64)               # verdict + list of the last 8 to the right of the frame
        fh = int(round(fw * GAME_H / GAME_W))
        if fh > avail:
            fh = max(u(120), avail)
            fw = int(round(fh * GAME_W / GAME_H))
        self.y_frame_head = y
        self.r_frame = (pad if not self.tall else (W - fw) // 2, y + self.head_h, fw, fh)
        top_h = self.head_h + fh
        if not self.tall:
            hx = pad + fw + u(16)
            vh = self.head_h + 3 * u(30) + self.gap
            self.r_vside = (hx, y, W - pad - hx, vh)
            self.r_hist = (hx, y + vh, W - pad - hx, top_h - vh)
        y += top_h + self.gap
        self.y_facts = y
        # leftover height (if any) goes to the gaps between sections, footer stays at the bottom
        self.extra = max(0, (H - y - below) // 6)

    def _layout_tiles(self):
        """Three stacked 16:9 tiles, each a complete slide on its own; every slot fixed, nothing crosses a boundary.
        tile 1: header + SoM still | verdict + the pick, executed line under the still
        tile 2: what TOD sees (2 columns) + papers + situation + today's rule
        tile 3: pick candidates + drop target + notes, last 8 actions, ground truth, latency"""
        u, W, pad = self._u, self.W, self.pad
        th = self.H // 3
        self.tile_h = th
        self.head_h = u(30)
        self.row_h = u(34)
        self.line_b = u(30)
        self.line_s = u(28)
        self.cand_h = u(32)
        self.gap = u(12)
        self.sit_lines = 4
        self.papers_lines = 2
        self.exec_lines = 2
        self.notes_lines = 3
        self.note_h = u(27)
        self.extra = 0
        # tile 1
        self.y_title = u(10)
        self.y_frame_head = u(52)
        fw = int(W * 0.66)
        fh = int(round(fw * GAME_H / GAME_W))
        self.r_frame = (pad, self.y_frame_head + self.head_h, fw, fh)
        cx = pad + fw + u(18)
        self.r_vside = (cx, self.y_frame_head, W - pad - cx, self.head_h + 3 * u(30))
        self.y_pick_side = self.y_frame_head + self.head_h + 3 * u(30) + u(14)
        self.y_exec = self.r_frame[1] + fh + u(10)
        # tile 2 / tile 3 start below their separator
        self.y_facts = th + u(12)
        self.y_cands = 2 * th + u(12)

    @property
    def som_size(self) -> tuple[int, int]:
        return self.r_frame[2], self.r_frame[3]

    # -- helpers
    _fit = V.Panel._fit
    _wrap = V.Panel._wrap

    def _head(self, d, y, title, right="", x=None, w=None):
        x = self.pad if x is None else x
        w = (self.W - 2 * self.pad) if w is None else w
        d.text((x, y), title, font=self.f_head, fill=ACCENT)
        if right:
            tw = d.textlength(title, font=self.f_head) + self._u(18)
            r = self._fit(d, right, self.f_small, w - tw)
            d.text((x + w - d.textlength(r, font=self.f_small), y + self._u(1)), r, font=self.f_small, fill=DIM)
        return y + self.head_h

    def _bar(self, d, x, y, w, h, frac, color):
        r = self._u(4)
        d.rounded_rectangle((x, y, x + w, y + h), radius=r, fill=(50, 54, 64))
        if frac and frac > 0:
            d.rounded_rectangle((x, y, x + max(int(w * min(frac, 1.0)), 2), y + h), radius=r, fill=color)

    # -- main
    def draw(self, cur: Scene, prev: Scene | None = None, t: float = 99.0) -> Image.Image:
        if prev is not None and prev.tick == cur.tick:
            prev = None
        settled = t >= ANIM_END + 0.05 or prev is None
        if settled and self._final is not None and self._final[0] is cur:
            return self._final[1]
        img = Image.new("RGB", (self.W, self.H), BG)
        d = ImageDraw.Draw(img)
        tt = t if not settled else 99.0
        if self.tiles:
            self._draw_tiles(img, d, cur, prev, tt)
            if settled:
                self._final = (cur, img)
            return img
        self._header(d, cur)
        self._frame(img, d, cur, prev, tt)
        if not self.tall:
            self._verdict_side(d, cur, prev, tt)
            self._hist_list(d, cur, prev, tt)
        y = self._facts(d, cur, prev, tt, self.y_facts)
        y = self._situation(d, cur, prev, tt, y + self.extra)
        if self.tall:
            y = self._verdict(d, cur, prev, tt, y + self.extra)
        y = self._pick(d, cur, prev, tt, y + self.extra)
        if self.tall:
            y = self._hist_strip(d, cur, prev, tt, y + self.extra)
        self.content_bottom = y
        self._footer(d, cur)
        if settled:
            self._final = (cur, img)
        return img

    # ---------------------------------------------------------------- three-tile column
    def _draw_tiles(self, img, d, cur: Scene, prev: Scene | None, t: float):
        th, W = self.tile_h, self.W
        # tile 1
        self._header(d, cur)
        self._frame(img, d, cur, prev, t)
        self._verdict_side(d, cur, prev, t)
        y1a = self._pick_side(d, cur, prev, t, self.y_pick_side)
        y1b = self._exec_line(d, cur, prev, t, self.y_exec)
        # tile 2
        y2 = self._facts(d, cur, prev, t, self.y_facts, right=f"tick {cur.tick} · amber = changed")
        y2 = self._situation(d, cur, prev, t, y2)
        # tile 3
        y3 = self._cands(d, cur, prev, t, self.y_cands)
        y3 = self._hist_strip(d, cur, prev, t, y3)
        y3 = self._footer_block(d, cur, y3)
        self.content_bottom = y3
        self.tile_ends = [max(y1a, y1b), y2, y3]
        for i in (1, 2):                    # hard separators between the tiles
            d.rectangle((0, i * th - 2, W, i * th + 1), fill=(70, 76, 90))

    def _pick_side(self, d, cur: Scene, prev: Scene | None, t: float, y: int) -> int:
        """Tile 1, beside the still: the chosen option -- #id + verb, p, object description, drop target."""
        u = self._u
        x, _, w, _ = self.r_vside
        pk = V.pick(cur.rec)
        y = self._head(d, y, "TOD's pick", x=x, w=w)
        ch = pk["chosen"] or []
        verb = str(ch[0]) if ch else ""
        cid = str(ch[1]) if len(ch) > 1 else ""
        pmap = {str(k): (p, desc) for k, p, desc in pk["top"]}
        p, desc = pmap.get(cid, (None, ""))
        if not desc:
            desc = (cur.rec.get("descriptions") or {}).get(cid, "")
        k = _phase(t, 0.05, 0.30) if prev is not None else 1.0
        if ch:
            head = f"#{cid}  {verb}" if cid and cid != "wait" else (verb or "wait")
            d.text((x, y), self._fit(d, head, self.f_title, w), font=self.f_title, fill=_mix(BG, AMBER, k))
            ptx = f"p {p:.2f}" if isinstance(p, (int, float)) else ""
            if ptx:
                d.text((x + w - d.textlength(ptx, font=self.f_mono), y + u(4)), ptx, font=self.f_mono, fill=FG)
            y += u(36)
            n_desc = 4 if pk["target"] else 5
            for ln in self._wrap(d, desc or "—", self.f_small, w, n_desc):
                d.text((x, y), ln, font=self.f_small, fill=_mix(BG, FG, k))
                y += self.line_s
            for tk, tp, _ in pk["target"][:1]:
                d.text((x, y), self._fit(d, f"→ drop on #{tk} ({tp:.2f})", self.f_small, w), font=self.f_small,
                       fill=AMBER)
                y += self.line_s
        else:
            d.text((x, y), "— (no pick this tick)", font=self.f_body, fill=DIM)
            y += self.line_s
        return y

    def _exec_line(self, d, cur: Scene, prev: Scene | None, t: float, y: int) -> int:
        """Tile 1, under the still: the executed action (2 lines, mono) + action probabilities."""
        pad, W = self.pad, self.W
        pk = V.pick(cur.rec)
        ex = pk["executed"]
        colx = RED if ex.startswith("vetoed") else GREEN
        k = _phase(t, 0.10, 0.35) if prev is not None else 1.0
        colx = _mix(BG, colx, k)
        lines = self._wrap(d, "Executed: " + (ex or "—"), self.f_mono, W - 2 * pad, self.exec_lines)
        for j, ln in enumerate(lines):
            d.text((pad, y + j * self.line_s), ln, font=self.f_mono, fill=colx)
        y += self.exec_lines * self.line_s
        act = "   ".join(f"{a} {p:.2f}" for a, p in pk["action"])
        d.text((pad, y), self._fit(d, "Action probabilities: " + (act or "—"), self.f_small, W - 2 * pad),
               font=self.f_small, fill=DIM)
        return y + self.line_s

    def _cands(self, d, cur: Scene, prev: Scene | None, t: float, y: int) -> int:
        """Tile 3: the top-3 candidates (2 description lines each), drop target, notes -- fixed slots."""
        u, pad, W = self._u, self.pad, self.W
        pk = V.pick(cur.rec)
        act = "  ".join(f"{a} {p:.2f}" for a, p in pk["action"])
        y = self._head(d, y, "TOD's pick · top candidates, p",
                       f"tick {cur.tick}" + (f" · action: {act}" if act else ""))
        prev_p = {}
        if prev is not None:
            prev_p = {str(k): p for k, p, _ in V.pick(prev.rec)["top"]}
        k_bar = _phase(t, 0.0, 0.32) if prev is not None else 1.0
        bx, bw, bh = pad + u(78), u(150), u(20)
        ch = pk["chosen"] or []
        chosen_id = str(ch[1]) if len(ch) > 1 else ""
        row = self.cand_h + self.line_s
        y0 = y
        for i, (kk, p, desc) in enumerate(pk["top"][:3]):
            yy = y0 + i * row
            is_c = str(kk) == chosen_id
            if is_c:
                d.rounded_rectangle((pad - u(6), yy - u(2), W - pad + u(4), yy + row - u(4)), radius=u(6),
                                    fill=(40, 44, 54))
            d.text((pad, yy), f"#{kk}" if kk != "wait" else "wait", font=self.f_bold, fill=AMBER if is_c else FG)
            p0 = prev_p.get(str(kk), 0.0)
            self._bar(d, bx, yy + u(6), bw, bh, p0 + (p - p0) * k_bar, ACCENT if is_c else (90, 100, 120))
            d.text((bx + bw + u(8), yy + u(3)), f"{p:.2f}", font=self.f_mono, fill=FG)
            tx = bx + bw + u(70)
            for j, ln in enumerate(self._wrap(d, desc or "—", self.f_small, W - pad - tx, 2)):
                d.text((tx, yy + u(3) + j * self.line_s), ln, font=self.f_small, fill=FG if is_c else DIM)
        if not pk["top"]:
            d.text((pad, y0), "— (no request-2 answer this tick)", font=self.f_body, fill=DIM)
        y = y0 + 3 * row
        tg = pk["target"][:1]
        if tg:
            kk, p, desc = tg[0]
            d.text((pad, y), self._fit(d, f"Drop target #{kk} ({p:.2f}): {desc}", self.f_small, W - 2 * pad),
                   font=self.f_small, fill=FG)
        else:
            d.text((pad, y), "Drop target: — (not a drag)", font=self.f_small, fill=FAINT)
        y += self.line_s + u(4)
        notes = pk["notes"]
        nl = self._wrap(d, "Notes: " + ("  ·  ".join(notes) if notes else "—"), self.f_note, W - 2 * pad,
                        self.notes_lines)
        for j, ln in enumerate(nl):
            d.text((pad, y + j * self.note_h), ln, font=self.f_note, fill=AMBER if notes else FAINT)
        return y + self.notes_lines * self.note_h + self.gap

    def _footer_block(self, d, cur: Scene, y: int) -> int:
        """Tile 3 bottom: ground truth (2 lines) + latency (2 lines), fixed slot."""
        pad, W, u = self.pad, self.W, self._u
        rec, prev = cur.rec, cur.prev_rec
        d.line((pad, y, W - pad, y), fill=(60, 64, 74), width=1)
        y += u(8)
        gt = rec.get("gt") or {}
        if gt.get("ok"):
            g = (f"Day {gt.get('day')} {gt.get('clock')}  processed {gt.get('day_processed')}  "
                 f"citations {gt.get('num_citations')}")
            if gt.get("entrant"):
                g = f"{gt.get('entrant')} should be {gt.get('correct')}  |  " + g
        else:
            g = f"screen {gt.get('screen', '?')}"
        for j, ln in enumerate(self._wrap(d, "Ground truth (not shown to TOD): " + g, self.f_small, W - 2 * pad, 2)):
            d.text((pad, y + j * self.line_s), ln, font=self.f_small, fill=FG)
        y += 2 * self.line_s

        def ms(k):
            v = rec.get(k)
            return f"{v / 1000:.1f}" if isinstance(v, (int, float)) else "-"
        per = ""
        if prev and isinstance(prev.get("time"), (int, float)) and isinstance(rec.get("time"), (int, float)):
            per = f"tick {rec['time'] - prev['time']:.1f}s = "
        lat = (f"Latency: {per}vision {ms('extract_ms')} + TOD state {ms('state_ms')} + papers {ms('doc_ms')} "
               f"+ TOD action {ms('tod_ms')} s (+ input, waits)")
        for j, ln in enumerate(self._wrap(d, lat, self.f_small, W - 2 * pad, 2)):
            d.text((pad, y + j * self.line_s), ln, font=self.f_small, fill=DIM)
        return y + 2 * self.line_s

    def _header(self, d, cur: Scene):
        pad, W = self.pad, self.W
        d.text((pad, self.y_title), "TOD plays Papers, Please — behind the scenes", font=self.f_title, fill=FG)
        tag = f"{cur.run}  tick {cur.tick}".strip()
        d.text((W - pad - d.textlength(tag, font=self.f_title), self.y_title), tag, font=self.f_title, fill=AMBER)

    # (a) SoM frame -- the still image TOD received
    def _frame(self, img, d, cur: Scene, prev: Scene | None, t: float):
        fx, fy, fw, fh = self.r_frame
        self._head(d, self.y_frame_head, "What TOD was shown · request 2", "numbered options", x=fx, w=fw)
        k = _phase(t, 0.0, TRANS_S) if prev is not None and prev.som is not None else 1.0
        if cur.som is not None:
            fr = cur.som
            if k < 1 and prev.som.size == cur.som.size:
                fr = Image.blend(prev.som, cur.som, k)
            img.paste(fr, (fx, fy))
        else:
            d.rectangle((fx, fy, fx + fw, fy + fh), fill=CARD)
            d.text((fx + self._u(12), fy + self._u(10)), "(frame not written yet)", font=self.f_small, fill=DIM)
        d.rectangle((fx - 1, fy - 1, fx + fw, fy + fh), outline=(58, 62, 72), width=max(1, self._u(2)))
        self._marker(img, cur, t)

    def _marker(self, img, cur: Scene, t: float):
        """Amber ring where TOD acted (drag: ring at the grab point + arrow to the drop point)."""
        if not cur.act_pts:
            return
        u = self._u
        fx, fy, fw, fh = self.r_frame
        k = _phase(t, 0.12, 0.40)
        if k <= 0:
            return
        pts = [(fx + a * fw, fy + b * fh) for a, b in cur.act_pts]
        x0, y0 = pts[0]
        m = u(50)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        box = (int(min(xs)) - m, int(min(ys)) - m, int(max(xs)) + m, int(max(ys)) + m)
        lay = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), (0, 0, 0, 0))
        d = ImageDraw.Draw(lay)
        ox, oy = box[0], box[1]
        a = int(255 * k)
        lw = max(2, u(4))
        if len(pts) == 2:
            x1, y1 = pts[1]
            xe, ye = x0 + (x1 - x0) * k, y0 + (y1 - y0) * k
            d.line((x0 - ox, y0 - oy, xe - ox, ye - oy), fill=(14, 16, 20, int(a * 0.6)), width=lw + u(4))
            d.line((x0 - ox, y0 - oy, xe - ox, ye - oy), fill=AMBER + (a,), width=lw)
            ang = math.atan2(ye - y0, xe - x0)
            hl = u(18)
            if math.hypot(xe - x0, ye - y0) > hl:
                d.polygon([(xe - ox, ye - oy),
                           (xe - ox - hl * math.cos(ang - 0.45), ye - oy - hl * math.sin(ang - 0.45)),
                           (xe - ox - hl * math.cos(ang + 0.45), ye - oy - hl * math.sin(ang + 0.45))],
                          fill=AMBER + (a,))
        r = int(u(30) - u(12) * k)
        cx, cy = x0 - ox, y0 - oy
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(14, 16, 20, int(a * 0.7)), width=lw + u(4))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=AMBER + (a,), width=lw)
        img.paste(lay, (ox, oy), lay)

    # (b) what TOD sees + papers
    def _facts(self, d, cur: Scene, prev: Scene | None, t: float, y: int, right: str = "") -> int:
        u, pad, W = self._u, self.pad, self.W
        y = self._head(d, y, "What TOD sees · request 1 answers, p", right or "amber = changed since last tick")
        prow = {k: v for k, _, _, v in V.state_rows(cur.prev_rec)} if cur.prev_rec else {}
        rows = V.state_rows(cur.rec)
        gx = u(12)
        colw = (W - 2 * pad - gx) // 2
        rh = self.row_h
        lab_w = u(176)
        flash = 1.0 - _phase(t, 0.15, ANIM_END + 0.1) if prev is not None else 0.0
        slide = _phase(t, 0.03, 0.30) if prev is not None else 1.0
        n_slot = 7                                   # fixed height: 7 rows per column (14 answers max)
        n_per_col = max(1, min(n_slot, (len(rows) + 1) // 2)) if len(rows) <= 2 * n_slot else (len(rows) + 1) // 2
        for i, (k, label, p, val) in enumerate(rows[: 2 * n_per_col]):
            # column-major: the left column reads top to bottom, then the right one
            cx = pad + (i // n_per_col) * (colw + gx)
            cy = y + (i % n_per_col) * rh
            changed = cur.prev_rec is not None and prow.get(k) != val
            bg = _mix(CHANGED_BG, FLASH_BG, flash) if changed else CARD
            d.rounded_rectangle((cx, cy, cx + colw, cy + rh - u(3)), radius=u(5), fill=bg)
            d.text((cx + u(8), cy + u(2)), self._fit(d, label, self.f_small, lab_w - u(12)), font=self.f_small,
                   fill=DIM)
            pt = V._pfmt(p)
            pw = d.textlength(pt, font=self.f_mono)
            vcol = AMBER if changed else FG
            vy = cy
            if changed and slide < 1:
                vy = cy + int(u(12) * (1 - slide))
                vcol = _mix(bg, vcol, slide)
            d.text((cx + lab_w, vy), self._fit(d, val, self.f_bold, colw - lab_w - pw - u(16)),
                   font=self.f_bold, fill=vcol)
            d.text((cx + colw - pw - u(8), cy + u(3)), pt, font=self.f_mono, fill=DIM)
        if not rows:
            d.text((pad, y), "— (no request-1 answers this tick)", font=self.f_body, fill=DIM)
        y_end = y + max(n_slot, n_per_col) * rh + u(2) + self.papers_lines * self.line_s
        y += n_per_col * rh + u(2)         # papers line right under the rows in use; the section end stays fixed
        pl = V.papers_line(cur.rec)
        lines = self._wrap(d, "Papers: " + (pl or "—"), self.f_small, W - 2 * pad, self.papers_lines)
        for ln in lines:
            d.text((pad, y), ln, font=self.f_small, fill=FG if pl else DIM)
            y += self.line_s
        return max(y, y_end) + self.gap

    # (c) situation + rule
    def _situation(self, d, cur: Scene, prev: Scene | None, t: float, y: int) -> int:
        pad, W = self.pad, self.W
        y = self._head(d, y, "Situation sentence sent to TOD · request 2")
        sit = V.situation(cur.rec) or ["(none this tick)"]
        k = 1.0
        if prev is not None and V.situation(prev.rec) != V.situation(cur.rec):
            k = _phase(t, 0.05, 0.32)
        col = _mix(BG, FG if V.situation(cur.rec) else DIM, k)
        left = self.sit_lines
        y0 = y
        for sent in sit[:2]:
            if left <= 0:
                break
            lines = self._wrap(d, sent, self.f_body, W - 2 * pad, left)
            left -= len(lines)
            for ln in lines:
                d.text((pad, y), ln, font=self.f_body, fill=col)
                y += self.line_b
        y = y0 + self.sit_lines * self.line_b
        rl = V.rule_line(cur.rec)
        for ln in self._wrap(d, "Rule today: " + (rl or "—"), self.f_small, W - 2 * pad, 2):
            d.text((pad, y), ln, font=self.f_small, fill=DIM)
            y += self.line_s
        y = y0 + self.sit_lines * self.line_b + 2 * self.line_s
        return y + self.gap

    # (d) verdict
    def _verdict(self, d, cur: Scene, prev: Scene | None, t: float, y: int) -> int:
        u, pad, W = self._u, self.pad, self.W
        probs, how = V.verdict(cur.rec)
        y = self._head(d, y, "TOD's verdict", how)
        bw, bh = W - 2 * pad, u(32)
        if probs:
            items = [it for it in VERDICT_ITEMS if it[0] in probs]

            def fracs(pr):
                if not pr:
                    return None
                tot = sum((pr.get(k) if pr.get(k) is not None else 1.0) for k, _, _ in items if k in pr) or 1.0
                return {k: ((pr.get(k) if pr.get(k) is not None else 1.0) / tot if k in pr else 0.0)
                        for k, _, _ in items}
            f1 = fracs(probs)
            f0 = fracs(V.verdict(prev.rec)[0]) if prev is not None else None
            k = _phase(t, 0.0, 0.32) if f0 else 1.0
            x0 = pad
            d.rectangle((pad, y, pad + bw, y + bh), fill=(50, 54, 64))
            for key, label, col in items:
                fr = f1[key] if not f0 else f0.get(key, 0.0) + (f1[key] - f0.get(key, 0.0)) * k
                w = int(bw * fr)
                if w > 0:
                    d.rectangle((x0, y, x0 + w, y + bh), fill=col)
                    tx = f"{label} {V._pfmt(probs[key])}" if probs[key] is not None else f"{label} (stored)"
                    if d.textlength(tx, font=self.f_bold) < w - u(10):
                        d.text((x0 + u(7), y + u(2)), tx, font=self.f_bold, fill=(10, 10, 10))
                x0 += w
            d.rectangle((pad, y, pad + bw, y + bh), outline=(200, 200, 200), width=1)
            y += bh + u(3)
            leg = "    ".join(f"{lab} {V._pfmt(probs[kk]) or 'stored'}" for kk, lab, _ in items)
            d.text((pad, y), self._fit(d, leg, self.f_small, bw), font=self.f_small, fill=DIM)
        else:
            d.rounded_rectangle((pad, y, pad + bw, y + bh), radius=u(5), fill=CARD)
            d.text((pad + u(10), y + u(2)), "—", font=self.f_bold, fill=DIM)
            y += bh + u(3)
        return y + self.line_s + self.gap

    # (e) pick
    def _pick(self, d, cur: Scene, prev: Scene | None, t: float, y: int) -> int:
        u, pad, W = self._u, self.pad, self.W
        pk = V.pick(cur.rec)
        act = "  ".join(f"{a} {p:.2f}" for a, p in pk["action"])
        y = self._head(d, y, "TOD's pick", f"action: {act}" if act else "")
        prev_p = {}
        if prev is not None:
            pp = V.pick(prev.rec)
            prev_p = {str(k): p for k, p, _ in pp["top"]}
        k_bar = _phase(t, 0.0, 0.32) if prev is not None else 1.0
        bx, bw, bh = pad + u(78), u(150), u(20)
        chosen_id = str((pk["chosen"] or ["", ""])[1]) if pk["chosen"] else ""
        y_c = y
        budget = 3 * self.cand_h + self.line_s       # 3 rows + the chosen row's second line
        for k, p, desc in pk["top"]:
            is_c = str(k) == chosen_id
            d.text((pad, y), f"#{k}" if k != "wait" else "wait", font=self.f_bold, fill=AMBER if is_c else FG)
            p0 = prev_p.get(str(k), 0.0)
            pv = p0 + (p - p0) * k_bar
            self._bar(d, bx, y + u(6), bw, bh, pv, ACCENT if is_c else (90, 100, 120))
            d.text((bx + bw + u(8), y + u(3)), f"{p:.2f}", font=self.f_mono, fill=FG)
            tx = bx + bw + u(70)
            used = y - y_c
            room_lines = 2 if is_c and used + self.cand_h + self.line_s <= budget else 1
            dl = self._wrap(d, desc or "—", self.f_small, W - pad - tx, room_lines)
            for j, ln in enumerate(dl):
                d.text((tx, y + u(3) + j * self.line_s), ln, font=self.f_small, fill=FG if is_c else DIM)
            y += self.cand_h + (len(dl) - 1) * self.line_s
        if not pk["top"]:
            d.text((pad, y), "— (no request-2 answer this tick)", font=self.f_body, fill=DIM)
        for k, p, desc in pk["target"][:1]:
            ln = self._fit(d, f"drop on #{k} ({p:.2f}): {desc}", self.f_small, W - 2 * pad)
            d.text((pad, y), ln, font=self.f_small, fill=FG)
            y += self.line_s
        y_end = y_c + budget + self.line_s + self.exec_lines * self.line_s + self.notes_lines * self.note_h
        ex = pk["executed"]
        colx = RED if ex.startswith("vetoed") else GREEN
        k = _phase(t, 0.10, 0.35) if prev is not None else 1.0
        colx = _mix(BG, colx, k)
        lines = self._wrap(d, "Executed: " + (ex or "—"), self.f_mono, W - 2 * pad, self.exec_lines)
        for ln in lines:
            d.text((pad, y), ln, font=self.f_mono, fill=colx)
            y += self.line_s
        y += (self.exec_lines - len(lines)) * self.line_s
        notes = [n for n in pk["notes"] if not n.startswith("vetoed")]
        nl = self._wrap(d, "  ·  ".join(notes), self.f_note, W - 2 * pad, self.notes_lines) if notes else []
        for ln in nl:
            d.text((pad, y), ln, font=self.f_note, fill=AMBER)
            y += self.note_h
        return max(y, y_end) + self.gap

    def _verdict_side(self, d, cur: Scene, prev: Scene | None, t: float):
        """Compact layout: the verdict as three labelled bars in the column beside the frame."""
        u = self._u
        x, y, w, h = self.r_vside
        probs, how = V.verdict(cur.rec)
        y = self._head(d, y, "TOD's verdict", how, x=x, w=w)
        p0s = (V.verdict(prev.rec)[0] or {}) if prev is not None else {}
        k = _phase(t, 0.0, 0.32) if prev is not None else 1.0
        best = max(probs, key=lambda kk: probs[kk] if probs[kk] is not None else 1.0) if probs else None
        lab_w = u(184)
        pw = d.textlength("stored", font=self.f_mono) + u(8)
        bx, bw = x + lab_w, w - lab_w - pw
        for key, label, col in VERDICT_ITEMS:
            rh = u(30)
            has = bool(probs) and key in probs
            p = probs.get(key) if has else None
            stored = has and p is None
            is_b = key == best
            d.text((x, y), self._fit(d, label, self.f_bold if is_b else self.f_small, lab_w - u(8)),
                   font=self.f_bold if is_b else self.f_small, fill=col if has else FAINT)
            p1 = 1.0 if stored else (p or 0.0)
            pp = p0s.get(key) if isinstance(p0s.get(key), (int, float)) else (1.0 if key in p0s else 0.0)
            pv = pp + (p1 - pp) * k
            self._bar(d, bx, y + u(6), bw, u(18), pv, col if has else (60, 64, 74))
            if is_b:
                d.rounded_rectangle((bx - 1, y + u(5), bx + bw + 1, y + u(25)), radius=u(4), outline=(200, 200, 200),
                                    width=1)
            tx = "stored" if stored else (V._pfmt(p) if has else "—")
            d.text((x + w - d.textlength(tx, font=self.f_mono), y + u(3)), tx, font=self.f_mono,
                   fill=FG if has else FAINT)
            y += rh

    # (f) last 8 actions -- list beside the frame (compact) or chip strip (tall)
    def _hist_items(self, cur: Scene):
        return [(V.history_item(h), h is cur.rec) for h in cur.history[-8:]]

    def _hist_list(self, d, cur: Scene, prev: Scene | None, t: float):
        u = self._u
        x, y, w, h = self.r_hist
        y = self._head(d, y, "Last 8 actions", x=x, w=w)
        d.text((x, y - u(3)), self._fit(d, "green = screen changed, red = no change", self.f_note, w), font=self.f_note,
               fill=DIM)
        y += u(26)
        items = self._hist_items(cur)
        n = 8
        step = (self.r_hist[1] + h - y) / n
        ch = int(step) - u(5)
        scroll = prev is not None and prev.tick != cur.tick
        off = step * (1 - _phase(t, 0.0, 0.30)) if scroll else 0.0
        start = n - len(items)
        y_clip = y
        for i, ((tk, label, eff), is_cur) in enumerate(items):
            cy = int(y + (start + i) * step + off)
            if cy + ch > self.r_hist[1] + h + 1:
                continue
            if cy < y_clip:
                continue
            col = (35, 80, 50) if eff is True else (95, 40, 38) if eff is False else CARD
            d.rounded_rectangle((x, cy, x + w, cy + ch), radius=u(5), fill=col,
                                outline=AMBER if is_cur else None, width=max(1, u(2)))
            d.text((x + u(8), cy + (ch - u(28)) // 2), tk, font=self.f_small, fill=AMBER if is_cur else DIM)
            lx = x + u(74)
            d.text((lx, cy + (ch - u(28)) // 2), self._fit(d, label, self.f_small, x + w - lx - u(8)),
                   font=self.f_small, fill=FG)

    def _hist_strip(self, d, cur: Scene, prev: Scene | None, t: float, y: int) -> int:
        u, pad, W = self._u, self.pad, self.W
        y = self._head(d, y, "Last 8 actions", "green = screen changed, red = no change")
        n = 8
        gx = u(6)
        cw = (W - 2 * pad - (n - 1) * gx) / n
        ch = u(84)
        items = self._hist_items(cur)
        scroll = prev is not None and prev.tick != cur.tick
        off = (cw + gx) * (1 - _phase(t, 0.0, 0.30)) if scroll else 0.0
        start = n - len(items)
        for i, ((tk, label, eff), is_cur) in enumerate(items):
            cx = pad + (start + i) * (cw + gx) + off
            if cx + cw > W - pad + 1:
                continue
            col = (35, 80, 50) if eff is True else (95, 40, 38) if eff is False else CARD
            d.rounded_rectangle((cx, y, cx + cw, y + ch), radius=u(5), fill=col,
                                outline=AMBER if is_cur else None, width=max(1, u(2)))
            d.text((cx + u(6), y + u(2)), tk, font=self.f_small, fill=AMBER if is_cur else FG)
            for j, ln in enumerate(self._wrap(d, label, self.f_note, cw - u(10), 2)):
                d.text((cx + u(6), y + u(30) + j * u(25)), ln, font=self.f_note, fill=FG)
        return y + ch + self.gap

    # (g) footer
    def _footer(self, d, cur: Scene):
        u, pad, W, s = self._u, self.pad, self.W, self.s
        rec, prev = cur.rec, cur.prev_rec
        fy = self.H - 2 * self.line_s - u(10)
        d.line((pad, fy - u(5), W - pad, fy - u(5)), fill=(60, 64, 74), width=1)
        gt = rec.get("gt") or {}
        if gt.get("ok"):
            g = (f"Day {gt.get('day')} {gt.get('clock')}  processed {gt.get('day_processed')}  "
                 f"citations {gt.get('num_citations')}")
            if gt.get("entrant"):
                g = f"{gt.get('entrant')} should be {gt.get('correct')}  |  " + g
        else:
            g = f"screen {gt.get('screen', '?')}"
        lab = "Ground truth (not shown to TOD): "
        d.text((pad, fy), lab, font=self.f_small, fill=DIM)
        lw = d.textlength(lab, font=self.f_small)
        d.text((pad + lw, fy), self._fit(d, g, self.f_small, W - 2 * pad - lw), font=self.f_small, fill=FG)
        fy += self.line_s

        def ms(k):
            v = rec.get(k)
            return f"{v / 1000:.1f}" if isinstance(v, (int, float)) else "-"
        per = ""
        if prev and isinstance(prev.get("time"), (int, float)) and isinstance(rec.get("time"), (int, float)):
            per = f"tick {rec['time'] - prev['time']:.1f}s = "
        lat = (f"{per}vision {ms('extract_ms')} + TOD state {ms('state_ms')} + papers {ms('doc_ms')} "
               f"+ TOD action {ms('tod_ms')} s (+ input, waits)")
        d.text((pad, fy), self._fit(d, lat, self.f_small, W - 2 * pad), font=self.f_small, fill=DIM)


# ---------------------------------------------------------------- sequencing / export
def scenes(reader: V.RunReader, panel: VideoPanel, ticks: list[int], want_raw: bool = False):
    for t in ticks:
        sc = build_scene(reader, t, panel.som_size, want_raw)
        if sc is not None:
            yield sc


def composite(raw_prev, raw_cur, panel_img: Image.Image, t: float, out_h: int,
              panel_h: int | None = None) -> np.ndarray:
    """RGB array: raw game frame (crossfaded) on the left, panel on the right, both at height out_h."""
    pan = np.asarray(panel_img)
    if pan.shape[0] != out_h:
        pan = cv2.resize(pan, (int(round(pan.shape[1] * out_h / pan.shape[0])), out_h), interpolation=cv2.INTER_AREA)
    k = _phase(t, 0.0, TRANS_S)
    left = raw_cur
    if raw_prev is not None and k < 1 and raw_prev.shape == raw_cur.shape:
        left = cv2.addWeighted(raw_prev, 1 - k, raw_cur, k, 0)
    left = cv2.cvtColor(left, cv2.COLOR_BGR2RGB)
    # the game keeps its on-screen size next to a taller column: game top-left, black below it (as on the display)
    gh = int(round(out_h * GAME_H / panel_h)) if panel_h and panel_h > GAME_H else out_h
    if left.shape[0] != gh:
        left = cv2.resize(left, (int(round(left.shape[1] * gh / left.shape[0])), gh), interpolation=cv2.INTER_AREA)
    if gh < out_h:
        left = np.vstack([left, np.zeros((out_h - gh, left.shape[1], 3), np.uint8)])
    return np.ascontiguousarray(np.hstack([left, pan]))


def export_video(reader: V.RunReader, panel: VideoPanel, ticks: list[int], out: str, hold: float = 0.5,
                 fps: int = 30, scale: float = 1.0, with_game: bool = True, crf: int = 20,
                 progress=print) -> dict:
    """Time-lapse mp4: one tick per `hold` seconds with the same transitions as follow mode."""
    import subprocess
    first = build_scene(reader, ticks[0], panel.som_size, with_game) if ticks else None
    if first is None:
        raise SystemExit("no ticks to export")
    out_h = int(round(panel.H * scale)) // 2 * 2
    probe = composite(first.raw, first.raw, panel.draw(first), 99, out_h, panel.H) if with_game and first.raw is not None \
        else np.asarray(panel.draw(first).resize((int(panel.W * scale) // 2 * 2, out_h)))
    H, W = probe.shape[:2]
    W -= W % 2
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    prev = None
    nframes = 0
    try:
        for i, sc in enumerate(scenes(reader, panel, ticks, with_game)):
            static = None
            n_hold = max(1, round((i + 1) * hold * fps) - round(i * hold * fps))   # exact total = ticks * hold
            for f in range(n_hold):
                t = f / fps
                if static is not None:
                    ff.stdin.write(static)
                    nframes += 1
                    continue
                pim = panel.draw(sc, prev, t)
                if with_game and sc.raw is not None:
                    arr = composite(prev.raw if prev is not None else None, sc.raw, pim, t, H, panel.H)
                else:
                    arr = np.asarray(pim.resize((W, H), Image.LANCZOS) if pim.size != (W, H) else pim)
                buf = np.ascontiguousarray(arr[:, :W]).tobytes()
                ff.stdin.write(buf)
                nframes += 1
                if t >= ANIM_END + 0.25:
                    static = buf
            prev = sc
            if progress and i % 25 == 0:
                progress(f"[export-video] tick {sc.tick} ({i + 1}/{len(ticks)})")
    finally:
        ff.stdin.close()
        rc = ff.wait()
    if rc != 0:
        raise SystemExit(f"ffmpeg failed rc={rc}")
    return {"out": out, "frames": nframes, "size": (W, H), "seconds": nframes / fps}
