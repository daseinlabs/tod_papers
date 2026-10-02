"""som.py -- Set-of-Mark annotation.

`annotate(frame_bgr, boxes, max_marks=60, excluded=None) -> (annotated_bgr, {id: Box})`

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

# share of the mark budget per box family when there are more boxes than marks.
# Without a quota, long text lines crowd out small untexted icons (levers,
# speakers, buttons), which are exactly the controls an agent most needs.
_QUOTA = {"text": 0.5, "icon": 0.3, "other": 0.2}


def _family(b: Box) -> str:
    if b.text and b.kind != "page_corner":
        return "text"
    if b.kind == "icon":
        return "icon"
    return "other"


def select(boxes: list[Box], max_marks: int = 60) -> list[Box]:
    """Keep at most max_marks boxes with a per-family quota (text / icon / other),
    unused quota flowing to the remaining families."""
    if len(boxes) <= max_marks:
        return list(boxes)
    fam: dict[str, list[Box]] = {"text": [], "icon": [], "other": []}
    for b in boxes:
        fam[_family(b)].append(b)
    fam["text"].sort(key=lambda b: (len(b.text) > 2, b.area), reverse=True)
    fam["icon"].sort(key=lambda b: (b.conf, b.area), reverse=True)
    fam["other"].sort(key=lambda b: (b.kind == "page_corner", b.area), reverse=True)
    take = {k: min(len(v), int(max_marks * _QUOTA[k])) for k, v in fam.items()}
    spare = max_marks - sum(take.values())
    for k in ("text", "icon", "other"):
        extra = min(spare, len(fam[k]) - take[k])
        take[k] += extra
        spare -= extra
    return [b for k in ("text", "icon", "other") for b in fam[k][: take[k]]]


def annotate(frame_bgr: np.ndarray, boxes: list[Box], max_marks: int = 60,
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
