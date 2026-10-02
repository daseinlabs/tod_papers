"""extract.py -- game-agnostic clickable-element extraction.

`extract(frame_bgr) -> list[Box]`

Stack (runs in .venv-extract / .venv-loop, Python 3.13):
  * RapidOCR (PP-OCRv3 det+rec, onnxruntime CPU) on a nearest-neighbour
    upscaled copy of the *native-resolution* frame (pixel fonts OCR much better
    when each art pixel is 2-3 device pixels, not 4+ and not 1).
  * OmniParser-style YOLO icon detector (models/icon_detect_model.pt,
    ultralytics, CUDA if available) for icons / buttons without text.
  * Cheap contour proposals on the native frame for large flat "panels"
    (documents, paper sheets, dialog boxes) -- these carry a detector kind
    'panel' and absorb any OCR text inside them as their label.

Then: text attached to containing icons/panels, NMS across all sources, and
boxes with neither text nor a detector class are dropped.

All returned coordinates are in the input frame's pixel space (client-relative
physical pixels when the frame comes from io_win.Grabber).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, asdict
from typing import Optional

import cv2
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ICON_MODEL = os.path.join(ROOT, "models", "icon_detect_model.pt")

NATIVE_W = 570          # Papers, Please native art width; used only as a hint
OCR_UPSCALE = 2         # nearest-neighbour upscale of the native frame for OCR
ICON_CONF = 0.15
OCR_MIN_CONF = 0.45
COLLAPSE_MAX_FRAC = 0.25
CORNER_MIN_FRAC = 0.01   # panels >= 1% of the frame get a page-corner proposal
CORNER_MAX = 6
CORNER_MIN_PX = 48
OCR_PROVIDER = "?"


@dataclass
class Box:
    x1: int
    y1: int
    x2: int
    y2: int
    text: str = ""
    kind: str = "text"   # 'text' | 'icon' | 'panel' | 'page_corner'
    conf: float = 0.0
    parent: str = ""     # page_corner: text of the paper it belongs to

    @property
    def w(self) -> int:
        return self.x2 - self.x1

    @property
    def h(self) -> int:
        return self.y2 - self.y1

    @property
    def area(self) -> int:
        return max(0, self.w) * max(0, self.h)

    @property
    def center(self) -> tuple[int, int]:
        return (self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# lazy model singletons
# --------------------------------------------------------------------------
_ocr = None
_yolo = None
_yolo_failed = False
LAST_TIMINGS: dict[str, float] = {}


def _get_ocr():
    global _ocr
    global OCR_PROVIDER
    if _ocr is None:
        # onnxruntime defaults to one thread per core; under CPU contention
        # (other agents, the game) 4 threads measured fastest (~1.5 s vs 2-4 s).
        import onnxruntime as ort
        import rapidocr_onnxruntime.utils as _U

        def _so():
            o = ort.SessionOptions()
            o.intra_op_num_threads = int(os.environ.get("TOD_OCR_THREADS", "4"))
            o.inter_op_num_threads = 1
            return o

        _U.SessionOptions = _so
        # onnxruntime-gpu: use CUDA EP when present (needs torch's bundled
        # CUDA/cuDNN DLLs on the DLL path), else CPU.
        try:
            import torch

            os.add_dll_directory(os.path.join(os.path.dirname(torch.__file__), "lib"))
        except Exception:
            pass
        ort.set_default_logger_severity(4)  # silence CUDA-EP fallback noise
        _real = ort.InferenceSession
        # Opt-in only: with the CUDA EP every new crop shape triggers a cuDNN
        # algo search, measured slower (OCR median 1.8 s) than CPU (0.5-1.3 s).
        use_gpu = ("CUDAExecutionProvider" in ort.get_available_providers()
                   and os.environ.get("TOD_OCR_GPU") == "1")

        def _sess(path, sess_options=None, providers=None, **kw):
            if use_gpu:
                providers = [("CUDAExecutionProvider", {"device_id": 0}), "CPUExecutionProvider"]
            return _real(path, sess_options=sess_options, providers=providers, **kw)

        _U.InferenceSession = _sess
        from rapidocr_onnxruntime import RapidOCR

        _ocr = RapidOCR()
        try:
            OCR_PROVIDER = _ocr.text_detector.infer.session.get_providers()[0]
        except Exception:
            OCR_PROVIDER = "CUDAExecutionProvider" if use_gpu else "CPUExecutionProvider"
        print(f"[extract] RapidOCR provider: {OCR_PROVIDER}")
    return _ocr


def _get_yolo():
    global _yolo, _yolo_failed
    if _yolo is None and not _yolo_failed:
        try:
            from ultralytics import YOLO

            _yolo = YOLO(ICON_MODEL)
        except Exception as e:  # detector optional; OCR + panels still work
            print(f"[extract] icon detector unavailable: {e}")
            _yolo_failed = True
    return _yolo


def warmup() -> None:
    dummy = np.zeros((320, 570, 3), np.uint8)
    cv2.putText(dummy, "WARMUP", (100, 160), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
    extract(dummy)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _native_scale(frame: np.ndarray) -> int:
    """Integer factor between frame and native art (4 for 2280x1280)."""
    s = int(round(frame.shape[1] / NATIVE_W))
    if s >= 1 and abs(frame.shape[1] - s * NATIVE_W) <= s and abs(frame.shape[0] - s * 320) <= s:
        return s
    return max(1, int(round(frame.shape[1] / 1140)))  # unknown game: aim ~1140 wide


def iou(a: Box, b: Box) -> float:
    ix = max(0, min(a.x2, b.x2) - max(a.x1, b.x1))
    iy = max(0, min(a.y2, b.y2) - max(a.y1, b.y1))
    inter = ix * iy
    u = a.area + b.area - inter
    return inter / u if u > 0 else 0.0


def _contain_frac(inner: Box, outer: Box) -> float:
    ix = max(0, min(inner.x2, outer.x2) - max(inner.x1, outer.x1))
    iy = max(0, min(inner.y2, outer.y2) - max(inner.y1, outer.y1))
    return (ix * iy) / inner.area if inner.area else 0.0


def _clean(t: str) -> str:
    return " ".join(t.replace("\n", " ").split())


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------


def _ocr_boxes(native: np.ndarray, s: int) -> list[Box]:
    up = cv2.resize(native, None, fx=OCR_UPSCALE, fy=OCR_UPSCALE, interpolation=cv2.INTER_NEAREST)
    res, _ = _get_ocr()(up, use_cls=False)
    out: list[Box] = []
    k = s / OCR_UPSCALE
    for pts, txt, conf in res or []:
        conf = float(conf)
        txt = _clean(txt)
        if conf < OCR_MIN_CONF or not txt:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        out.append(Box(int(min(xs) * k), int(min(ys) * k), int(max(xs) * k), int(max(ys) * k), txt, "text", conf))
    return out


def _icon_boxes(frame: np.ndarray) -> list[Box]:
    m = _get_yolo()
    if m is None:
        return []
    try:
        import torch

        dev = 0 if torch.cuda.is_available() else "cpu"
    except Exception:
        dev = "cpu"
    r = m.predict(frame, conf=ICON_CONF, imgsz=1280, verbose=False, device=dev)[0]
    out = []
    for b in r.boxes:
        x1, y1, x2, y2 = (int(v) for v in b.xyxy[0].tolist())
        out.append(Box(x1, y1, x2, y2, "", "icon", float(b.conf)))
    return out


def _panel_boxes(native: np.ndarray, s: int) -> list[Box]:
    """Large flat-ish rectangles (documents, sheets, dialog panels) on the
    native frame: regions of near-uniform colour that differ from their
    surroundings. Cheap and game-agnostic."""
    H, W = native.shape[:2]
    gray = cv2.cvtColor(native, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 30, 90)
    edges = cv2.dilate(edges, np.ones((2, 2), np.uint8))
    cnts, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    total = W * H
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        a = w * h
        if a < 0.004 * total or a > 0.35 * total:
            continue
        if w < 12 or h < 8:
            continue
        # rectangularity: contour area relative to bbox (documents are boxy)
        rect = cv2.contourArea(cv2.convexHull(c)) / max(1, a)
        if rect < 0.6:
            continue
        # must be an *object* that stands out from what surrounds it (paper on a
        # desk, a dialog plate). Groups of text on a flat background (menu lists)
        # have interior ~= surroundings and are dropped; their text boxes remain.
        m = max(2, min(w, h) // 6)
        X1, Y1, X2, Y2 = max(0, x - m), max(0, y - m), min(W, x + w + m), min(H, y + h + m)
        ring = gray[Y1:Y2, X1:X2].astype(np.float32)
        ring_sum = ring.sum() - gray[y:y + h, x:x + w].astype(np.float32).sum()
        ring_n = ring.size - w * h
        if ring_n <= 0:
            continue
        contrast = abs(float(gray[y:y + h, x:x + w].mean()) - ring_sum / ring_n)
        if contrast < 18:
            continue
        out.append(Box(x * s, y * s, (x + w) * s, (y + h) * s, "", "panel", round(rect * 0.5, 3)))
    return out


# --------------------------------------------------------------------------
# merge
# --------------------------------------------------------------------------

_PRI = {"text": 3, "icon": 2, "panel": 1}


def _merge(texts: list[Box], icons: list[Box], panels: list[Box], frame_area: int) -> list[Box]:
    # 1. merge text fragments on the same line that nearly touch (pixel fonts
    #    sometimes split a word: "1.4." + "124-5").
    #    Repeat pairwise until stable (word order on a line is by x, not y1 jitter).
    texts = list(texts)
    changed = True
    while changed:
        changed = False
        texts.sort(key=lambda b: (b.x1, b.y1))
        for i in range(len(texts)):
            for j in range(len(texts)):
                if i == j:
                    continue
                p, t = texts[i], texts[j]
                hh = max(p.h, t.h)
                same_line = abs(p.center[1] - t.center[1]) < 0.4 * hh and 0.5 < p.h / max(1, t.h) < 2.0
                gap = t.x1 - p.x2
                if same_line and p.x1 <= t.x1 and -0.5 * hh < gap < 1.0 * hh:
                    texts[i] = Box(min(p.x1, t.x1), min(p.y1, t.y1), max(p.x2, t.x2), max(p.y2, t.y2),
                                   f"{p.text} {t.text}", "text", min(p.conf, t.conf))
                    del texts[j]
                    changed = True
                    break
            if changed:
                break

    # 2. icons overlapping a text box mostly -> drop icon, text wins (it has a label).
    keep_icons = []
    for ic in icons:
        if any(iou(ic, t) > 0.4 or _contain_frac(ic, t) > 0.7 for t in texts):
            continue
        # give the icon any text that sits inside it
        inside = [t for t in texts if _contain_frac(t, ic) > 0.6]
        if inside:
            ic.text = " ".join(t.text for t in sorted(inside, key=lambda b: (b.y1, b.x1)))[:80]
        keep_icons.append(ic)

    # 3. panels: dedupe (dilated edges give inner+outer contours of one object)
    panels = sorted(panels, key=lambda b: b.area, reverse=True)
    uniq: list[Box] = []
    for pn in panels:
        if any(iou(pn, u) > 0.6 for u in uniq):
            continue
        uniq.append(pn)
    panels = uniq
    #    stitch vertically stacked pieces of one paper (a dotted/ruled line across
    #    a document splits its contour): same left/right edges, touching.
    tol = max(8, int(0.006 * (frame_area ** 0.5)))
    merged_any = True
    while merged_any:
        merged_any = False
        for a in panels:
            for b in panels:
                if a is b:
                    continue
                if abs(a.x1 - b.x1) <= tol and abs(a.x2 - b.x2) <= tol and 0 <= b.y1 - a.y2 <= tol:
                    a.y2 = b.y2
                    a.x1, a.x2 = min(a.x1, b.x1), max(a.x2, b.x2)
                    a.conf = max(a.conf, b.conf)
                    panels.remove(b)
                    merged_any = True
                    break
            if merged_any:
                break
    #    A small panel (document / sheet / note, < COLLAPSE_MAX_FRAC of the frame)
    #    holding >= 2 text lines absorbs those lines: the paper becomes ONE
    #    markable object labelled with its text, instead of a dozen unclickable
    #    line boxes. Each line goes to its smallest containing panel.
    inside_of = {id(pn): [t for t in texts if _contain_frac(t, pn) > 0.6] for pn in panels}
    absorbed: dict[int, list[Box]] = {id(pn): [] for pn in panels}
    absorbed_ids = set()
    for t in texts:
        owners = [pn for pn in panels if pn.area < COLLAPSE_MAX_FRAC * frame_area
                  and len(inside_of[id(pn)]) >= 2 and t in inside_of[id(pn)]]
        if owners:
            o = min(owners, key=lambda b: b.area)
            absorbed[id(o)].append(t)
            absorbed_ids.add(id(t))
    keep_panels = []
    for pn in panels:
        inside = inside_of[id(pn)]
        if inside and len(inside) == 1 and inside[0].area > 0.5 * pn.area:
            continue  # single-line plate: the text box already covers it
        if not inside and any(o is not pn and o.area > pn.area and _contain_frac(pn, o) > 0.8 for o in panels):
            continue  # untexted sub-part of a bigger object (logo letters, photo inside a doc)
        lab = absorbed[id(pn)] or inside
        if lab:
            pn.text = " ".join(t.text for t in sorted(lab, key=lambda b: (b.y1, b.x1)))[:120]
        keep_panels.append(pn)
    texts = [t for t in texts if id(t) not in absorbed_ids]

    cands = texts + keep_icons + keep_panels
    # 4. drop unlabeled boxes with no detector class (defensive; every source sets kind)
    cands = [b for b in cands if b.text or b.kind in ("icon", "panel")]
    # 5. NMS: priority then conf
    cands.sort(key=lambda b: (_PRI.get(b.kind, 0), b.conf, b.area), reverse=True)
    out: list[Box] = []
    for b in cands:
        if any(iou(b, k) > 0.5 for k in out):
            continue
        out.append(b)
    # 6. page corners: multi-page papers (bulletin, rulebook, passport) flip by
    #    clicking/dragging their bottom-right corner, which no detector reports.
    #    Propose that corner as its own box for every paper-like panel.
    docs = sorted((b for b in out if b.kind == "panel" and b.area >= CORNER_MIN_FRAC * frame_area),
                  key=lambda b: b.area, reverse=True)[:CORNER_MAX]
    for d in docs:
        c = max(CORNER_MIN_PX, int(0.12 * min(d.w, d.h)))
        cb = Box(d.x2 - c, d.y2 - c, d.x2, d.y2, "", "page_corner", d.conf)
        cb.parent = d.text
        if any(k.kind == "page_corner" and iou(cb, k) > 0.3 for k in out):
            continue
        out.append(cb)
    return out


def extract(frame_bgr: np.ndarray) -> list[Box]:
    t0 = time.perf_counter()
    s = _native_scale(frame_bgr)
    H, W = frame_bgr.shape[:2]
    native = cv2.resize(frame_bgr, (W // s, H // s), interpolation=cv2.INTER_NEAREST) if s > 1 else frame_bgr
    t1 = time.perf_counter()
    texts = _ocr_boxes(native, s)
    t2 = time.perf_counter()
    icons = _icon_boxes(frame_bgr)
    t3 = time.perf_counter()
    panels = _panel_boxes(native, s)
    boxes = _merge(texts, icons, panels, W * H)
    # pad text boxes a little so markers/clicks don't sit on the glyph edge
    for b in boxes:
        if b.kind == "text":
            p = max(2, s)
            b.x1, b.y1, b.x2, b.y2 = max(0, b.x1 - p), max(0, b.y1 - p), min(W - 1, b.x2 + p), min(H - 1, b.y2 + p)
    t4 = time.perf_counter()
    LAST_TIMINGS.clear()
    LAST_TIMINGS.update(ocr_ms=(t2 - t1) * 1e3, icon_ms=(t3 - t2) * 1e3,
                        panel_merge_ms=(t4 - t3) * 1e3, total_ms=(t4 - t0) * 1e3)
    return boxes


if __name__ == "__main__":
    import sys

    img = cv2.imread(sys.argv[1])
    warmup()
    bs = extract(img)
    print(LAST_TIMINGS)
    for b in bs:
        print(b)
