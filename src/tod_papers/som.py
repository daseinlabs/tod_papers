"""som.py -- Set-of-Mark annotation.

`annotate(frame_bgr, boxes, max_marks=40, excluded=None) -> (annotated_bgr, {id: Box})`

Before drawing, `select()` collapses same-caption duplicates (overlap > 50% of
the smaller box -> keep the larger) and prunes untexted, weakly-labelled
icon/panel boxes down to `max_marks`.

Draws each box outline plus a high-contrast numbered tag (white digits on a
solid coloured plate with black border) at the box's top-left corner (moved
inside/below when it would leave the frame). IDs are 1..N assigned in
reading order (top-to-bottom, left-to-right) so numbering carries no hint
about which element is "important".

`excluded(box) -> bool` marks boxes the loop has temporarily ruled out (e.g.
clicked twice with no effect): they keep their number but get a grey plate and
a strike-through so the image agrees with the reduced option list.
"""
from __future__ import annotations

from typing import Callable, Optional

import cv2
import numpy as np

from .extract import Box

# distinct, saturated plate colours (BGR); cycled by id
_COLORS = [
    (0, 0, 230), (0, 160, 0), (230, 60, 0), (0, 140, 255), (200, 0, 200),
    (160, 160, 0), (0, 200, 200), (90, 0, 160), (0, 90, 200), (140, 70, 0),
]
_GREY = (128, 128, 128)

# captions that come from the open-vocabulary detector's vocabulary (the
# interactive things: lever, speaker, stamps, papers ...). CLIP-only labels on
# scenery fragments ("pole", "road barrier", "possibly door") are weaker.
try:
    from .extract import GDINO_VOCAB as _GV
    STRONG_CAPS = {c for c, _ in _GV.values()} | {"page corner"}
except Exception:  # pragma: no cover - older extract.py
    STRONG_CAPS = set()


def _overlap(a: Box, b: Box) -> float:
    """intersection / area of the smaller box"""
    ix = max(0, min(a.x2, b.x2) - max(a.x1, b.x1))
    iy = max(0, min(a.y2, b.y2) - max(a.y1, b.y1))
    m = min(a.area, b.area)
    return ix * iy / m if m else 0.0


def collapse(boxes: list[Box], thresh: float = 0.5) -> list[Box]:
    """Boxes sharing a caption and overlapping > thresh (of the smaller) collapse
    to the larger one. Never collapses page corners, and keeps a smaller box whose
    OCR text is not already part of the larger box's text."""
    order = sorted(boxes, key=lambda b: -b.area)
    kept: list[Box] = []
    for b in order:
        cap = getattr(b, "caption", "")
        if cap and b.kind != "page_corner":
            dup = False
            for k in kept:
                if k.kind == "page_corner" or getattr(k, "caption", "") != cap or _overlap(b, k) <= thresh:
                    continue
                if b.text and b.text.lower() not in k.text.lower():
                    continue
                dup = True
                break
            if dup:
                continue
        kept.append(b)
    return [b for b in boxes if any(b is k for k in kept)]


def _drop_tier(b: Box) -> int | None:
    """Prune order for the mark cap: lower tiers go first; None = never pruned
    (text, page corners, detector objects, detector-vocabulary captions)."""
    cap = getattr(b, "caption", "")
    if b.text or b.kind == "page_corner" or b.kind not in ("icon", "panel"):
        return None
    if not cap:
        return 0
    if cap.startswith("possibly "):
        return 1
    if cap not in STRONG_CAPS:
        return 2
    return None


def select(boxes: list[Box], max_marks: int = 40) -> list[Box]:
    """Collapse same-caption duplicates, then (if still over max_marks) drop
    untexted icon/panel boxes - uncaptioned first, then weak 'possibly' labels,
    then non-detector scenery labels; smallest first within a tier. Text, page
    corners, detector objects and detector-captioned boxes are never dropped, so
    the cap is soft."""
    out = collapse(boxes)
    if len(out) <= max_marks:
        return out
    cand = sorted((b for b in out if _drop_tier(b) is not None), key=lambda b: (_drop_tier(b), b.area))
    drop = {id(b) for b in cand[: len(out) - max_marks]}
    return [b for b in out if id(b) not in drop]


def annotate(frame_bgr: np.ndarray, boxes: list[Box], max_marks: int = 40,
             excluded: Optional[Callable[[Box], bool]] = None):
    H, W = frame_bgr.shape[:2]
    kept = select(boxes, max_marks)
    kept.sort(key=lambda b: (b.y1 // max(1, H // 40), b.x1))
    out = frame_bgr.copy()
    # scale drawing with resolution so tags stay legible after downscaling to ~1140 wide
    k = max(1.0, W / 1140)
    font = cv2.FONT_HERSHEY_SIMPLEX
    fscale = 0.75 * k
    thick = max(2, int(round(2 * k)))
    line = max(2, int(round(1.5 * k)))
    idmap: dict[int, Box] = {}
    placed: list[tuple[int, int, int, int]] = []
    off = [bool(excluded and excluded(b)) for b in kept]

    # outlines first so tags draw on top
    for i, b in enumerate(kept, 1):
        col = _GREY if off[i - 1] else _COLORS[(i - 1) % len(_COLORS)]
        cv2.rectangle(out, (b.x1, b.y1), (b.x2, b.y2), (0, 0, 0), line + 2)
        cv2.rectangle(out, (b.x1, b.y1), (b.x2, b.y2), col, line)

    for i, b in enumerate(kept, 1):
        idmap[i] = b
        col = _GREY if off[i - 1] else _COLORS[(i - 1) % len(_COLORS)]
        label = str(i)
        (tw, th), base = cv2.getTextSize(label, font, fscale, thick)
        pad = int(4 * k)
        pw, ph = tw + 2 * pad, th + base + 2 * pad
        # candidate positions: above-left, inside top-left, below-left, inside top-right
        cands = [(b.x1, b.y1 - ph), (b.x1, b.y1), (b.x1, b.y2), (b.x2 - pw, b.y1)]
        best = None
        for (x, y) in cands:
            x = int(min(max(0, x), W - pw))
            y = int(min(max(0, y), H - ph))
            r = (x, y, x + pw, y + ph)
            overlap = sum(max(0, min(r[2], p[2]) - max(r[0], p[0])) * max(0, min(r[3], p[3]) - max(r[1], p[1])) for p in placed)
            if best is None or overlap < best[0]:
                best = (overlap, r)
            if overlap == 0:
                break
        x1, y1, x2, y2 = best[1]
        placed.append(best[1])
        cv2.rectangle(out, (x1, y1), (x2, y2), col, -1)
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 0, 0), max(1, line))
        org = (x1 + pad, y2 - pad - base // 2)
        cv2.putText(out, label, org, font, fscale, (0, 0, 0), thick + 3, cv2.LINE_AA)
        cv2.putText(out, label, org, font, fscale, (255, 255, 255), thick, cv2.LINE_AA)
        if off[i - 1]:  # strike-through: "ruled out for now"
            cv2.line(out, (x1, y1), (x2, y2), (0, 0, 0), thick + 1, cv2.LINE_AA)
            cv2.line(out, (b.x1, b.y1), (b.x2, b.y2), _GREY, line, cv2.LINE_AA)
    return out, idmap
