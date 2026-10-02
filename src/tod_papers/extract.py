"""extract.py -- game-agnostic clickable-element extraction.

`extract(frame_bgr) -> list[Box]`, `describe(box) -> str`

Stack (runs in .venv-loop, Python 3.13; see docs/extraction.md):
  * OCR: RapidOCR 3.x -- PP-OCRv4 mobile det on a 2x nearest-neighbour copy
    of the *native-resolution* frame (pixel art is exactly 4x replicated, so
    frame[::4, ::4] is lossless), then PP-OCRv5 mobile rec on each line
    re-cropped from the native frame with 2 px padding and upscaled 3x
    nearest-neighbour. Runs on CPU in a worker thread, overlapped with the
    GPU stages. Falls back to rapidocr_onnxruntime 1.2.3 (PP-OCRv3) when
    rapidocr 3.x / the models are missing.
  * OmniParser-style YOLO icon detector (models/icon_detect_model.pt,
    ultralytics, CUDA) for icons / buttons without text.
  * Contour proposals on the native frame for large flat "panels"
    (documents, sheets, dialog boxes); they absorb OCR text inside them.
  * Labels ("caption"): Grounding DINO tiny (open-vocabulary detector, whole
    frame, fp16 CUDA) with a fixed object vocabulary; its detections caption
    the boxes they cover and add boxes YOLO missed (shutter lever, horn,
    page corner ...). Boxes still without text or caption get a CLIP
    ViT-B/16 zero-shot label on their crop.

Then: text attached to containing icons/panels, NMS across all sources,
contained duplicates dropped, a page-corner box per paper.

All returned coordinates are in the input frame's pixel space (client-relative
physical pixels when the frame comes from io_win.Grabber).
"""
from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict

import cv2
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MODELS = os.path.join(ROOT, "models")
ICON_MODEL = os.path.join(MODELS, "icon_detect_model.pt")
OCR_DET_MODEL = os.path.join(MODELS, "rapidocr", "ch_PP-OCRv4_det_mobile.onnx")
OCR_REC_MODEL = os.path.join(MODELS, "rapidocr", "ch_PP-OCRv5_rec_mobile.onnx")
OCR_REC_EN_MODEL = os.path.join(MODELS, "rapidocr", "en_PP-OCRv4_rec_mobile.onnx")
LEXICON_FILE = os.path.join(MODELS, "grounding-dino-tiny", "vocab.txt")  # BERT uncased word list
GDINO_MODEL = os.path.join(MODELS, "grounding-dino-tiny")
CLIP_MODEL = os.path.join(MODELS, "clip-vit-base-patch16")

NATIVE_W = 570          # Papers, Please native art width; used only as a hint
OCR_UPSCALE = 2         # nearest-neighbour upscale of the native frame for text *detection*
OCR_REC_UPSCALE = 3     # bilinear upscale of each native line crop for *recognition*
OCR_REC_PAD = 2         # native px of context around each detected line
OCR_LEX_BONUS = 0.15    # score bonus x fraction of dictionary words when picking between
                        # the two recognisers' readings of a line
OCR_SECOND_MAX_WORDS = 4
ICON_CONF = 0.15
OCR_MIN_CONF = 0.45
COLLAPSE_MAX_FRAC = 0.25
CORNER_MIN_FRAC = 0.01   # panels >= 1% of the frame get a page-corner proposal
CORNER_MAX = 6
CORNER_MIN_PX = 48
OCR_PROVIDER = "?"
OCR_ENGINE = "?"

# open-vocabulary labelling -------------------------------------------------
LABELS = os.environ.get("TOD_LABELS", "1") != "0"      # TOD_LABELS=0 disables GDINO + CLIP
GDINO_SHORT = int(os.environ.get("TOD_GDINO_SIZE", "640"))  # short side fed to GDINO
GDINO_MAX_FRAC = 0.2     # ignore detections covering more of the frame than this
# query phrase -> (caption, min score). Grounding DINO scores are low on pixel
# art; thresholds were set on the saved booth frames (docs/extraction.md).
GDINO_VOCAB = {
    # fires on the whole paper whose corner is folded (0.25-0.6 on the booth
    # papers), rarely on the corner itself -> page_corner at its bottom-right
    "folded page corner": ("folded page corner", 0.20),
    "rubber stamp": ("rubber stamp", 0.30),
    # "lever handle" hit a fence post; "yellow lever" finds the shutter lever
    "yellow lever": ("lever handle", 0.25),
    "loudspeaker": ("speaker/horn", 0.23),
    "horn speaker": ("speaker/horn", 0.23),
    "passport": ("passport booklet", 0.30),
    "document": ("paper document", 0.30),
    "person face": ("person face", 0.30),
    "button": ("button", 0.30),
    "clock": ("clock", 0.30),
    "ticket": ("ticket", 0.30),
    "bulletin board": ("bulletin board", 0.35),
}
# Counter-strip pass: a second, small Grounding DINO run on just the counter in
# front of the entrant window (Papers, Please layout; frame fractions
# x1,y1,x2,y2), with its own prompt so the main prompt's scores are untouched
# (adding phrases to one prompt shifts every score: the bulletin's tape turned
# into a "loudspeaker"). On the full frame the closed passport lying there
# scores at most "rubber stamp" 0.25 / "green passport" 0.50 -- and "green
# passport" also fires 0.56 on the green APPROVED stamp -- so only inside the
# strip does a document-like hit become "document on counter".
COUNTER_QUERIES = ("green passport", "small closed booklet", "passport", "document", "folded paper")
COUNTER_STRIP = (0.0, 0.645, 0.315, 0.865)
COUNTER_DOC_MIN = 0.25         # passport 0.34-0.36 on 221705 raw_0027..39; empty counter <= 0.19
COUNTER_DOC_MAX_FRAC = 0.35   # of the strip area (whole-strip hits are the counter itself)
COUNTER_DOC_MIN_PX = 40       # min side, frame px
COUNTER_CAP = "document on counter"
COUNTER_PASS = os.environ.get("TOD_COUNTER_PASS", "1") != "0"
COUNTER_SHORT = 320           # short side the strip crop is resized to for GDINO
# detections of these (large ones) delimit a paper -> page corner at its bottom-right
PAPER_CAPS = {"folded page corner", "passport booklet", "paper document", "ticket"}
PAPER_MIN_FRAC = 0.03
# CLIP zero-shot vocabulary for crops nothing else labelled
# (no lever / speaker / page-corner classes here: CLIP put those on fence posts
# and slats; Grounding DINO owns them)
# "passport booklet" was dropped: CLIP put it on the rulebook (0.47); with
# closed/open passport + rulebook classes the rulebook reads "rulebook / ring
# binder" 0.64 and the open passport on the desk "open passport" 0.74.
CLIP_VOCAB = ["red rubber stamp", "green rubber stamp", "closed passport", "open passport",
              "rulebook / ring binder", "paper document", "book",
              "person face", "button", "clock", "ticket", "bulletin board", "crowd of people", "soldier",
              "fence", "pole", "wall", "concrete ground", "road barrier", "window shutter", "drawer", "tray",
              "weighing scale", "speaker grille", "envelope", "map", "building", "logo", "arrow",
              "dark empty background", "desk", "booth", "window", "door", "car", "flag", "photo"]
CLIP_MIN_P = 0.25
# document labels: CLIP is better at telling passport / rulebook / bulletin
# apart than Grounding DINO, so for boxes GDINO calls a paper/passport CLIP
# gets a second say, and wins when it picks one of these with p >= CLIP_DOC_P.
DOC_CAPS = {"closed passport", "open passport", "rulebook / ring binder", COUNTER_CAP}
CLIP_DOC_P = 0.4
CLIP_OPEN_TEXT_P = 0.65


@dataclass
class Box:
    x1: int
    y1: int
    x2: int
    y2: int
    text: str = ""
    kind: str = "text"   # 'text' | 'icon' | 'panel' | 'object' | 'page_corner'
    conf: float = 0.0
    parent: str = ""     # page_corner: text of the paper it belongs to
    caption: str = ""    # short visual label (open-vocab detector / CLIP), '' if none

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
_ocr3 = None
_ocr3_failed = False
_yolo = None
_yolo_failed = False
_gdino = None
_clip = None
_labels_failed = False
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="extract-ocr")
LAST_TIMINGS: dict[str, float] = {}
_LAST_WH: tuple[int, int] = (0, 0)
VERBOSE = os.environ.get("TOD_EXTRACT_VERBOSE", "1") == "1"  # one timing line per extract()


def _ocr_threads() -> int:
    return int(os.environ.get("TOD_OCR_THREADS", "8"))


def _add_torch_dlls() -> None:
    try:
        import torch

        os.add_dll_directory(os.path.join(os.path.dirname(torch.__file__), "lib"))
    except Exception:
        pass


_ocr3_en = None
_lexicon: set[str] = set()


def _get_ocr3():
    """RapidOCR 3.x: PP-OCRv4 det + PP-OCRv5 rec, plus the PP-OCRv4 English
    recogniser as a second opinion (preferred stack)."""
    global _ocr3, _ocr3_en, _ocr3_failed, OCR_PROVIDER, OCR_ENGINE, _lexicon
    if _ocr3 is None and not _ocr3_failed:
        try:
            if not (os.path.exists(OCR_DET_MODEL) and os.path.exists(OCR_REC_MODEL)):
                raise FileNotFoundError(OCR_REC_MODEL)
            import logging

            from rapidocr import LangDet, LangRec, ModelType, OCRVersion, RapidOCR

            use_gpu = os.environ.get("TOD_OCR_GPU") == "1"  # opt-in, CPU measured faster
            if use_gpu:
                _add_torch_dlls()
            _ocr3 = RapidOCR(params={
                "Global.use_cls": False,
                "Global.log_level": "error",
                "Det.model_path": OCR_DET_MODEL,
                "Det.ocr_version": OCRVersion.PPOCRV4, "Det.lang_type": LangDet.CH,
                "Det.model_type": ModelType.MOBILE,
                "Det.limit_side_len": 1280, "Det.limit_type": "max",
                "Rec.model_path": OCR_REC_MODEL,
                "Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.CH,
                "Rec.model_type": ModelType.MOBILE,
                "EngineConfig.onnxruntime.intra_op_num_threads": _ocr_threads(),
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                "EngineConfig.onnxruntime.use_cuda": use_gpu,
            })
            if os.path.exists(OCR_REC_EN_MODEL) and os.environ.get("TOD_OCR_SECOND", "1") != "0":
                _ocr3_en = RapidOCR(params={
                    "Global.use_cls": False, "Global.log_level": "error",
                    "Det.model_path": OCR_DET_MODEL,
                    "Det.ocr_version": OCRVersion.PPOCRV4, "Det.lang_type": LangDet.CH,
                    "Det.model_type": ModelType.MOBILE,
                    "Rec.model_path": OCR_REC_EN_MODEL,
                    "Rec.ocr_version": OCRVersion.PPOCRV4, "Rec.lang_type": LangRec.EN,
                    "Rec.model_type": ModelType.MOBILE,
                    "EngineConfig.onnxruntime.intra_op_num_threads": _ocr_threads(),
                    "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                    "EngineConfig.onnxruntime.use_cuda": use_gpu,
                })
                try:
                    with open(LEXICON_FILE, encoding="utf-8") as f:
                        _lexicon = {w.strip() for w in f if w.strip().isalpha() and len(w.strip()) >= 2}
                except OSError:
                    _lexicon = set()
            logging.getLogger("RapidOCR").setLevel(logging.ERROR)
            OCR_ENGINE = "rapidocr3 PP-OCRv4det+PP-OCRv5rec" + ("+PP-OCRv4enrec" if _ocr3_en else "")
            OCR_PROVIDER = "CUDAExecutionProvider" if use_gpu else "CPUExecutionProvider"
            print(f"[extract] OCR: {OCR_ENGINE} ({OCR_PROVIDER})")
        except Exception as e:
            print(f"[extract] rapidocr 3.x unavailable ({e!r}); falling back to rapidocr_onnxruntime")
            _ocr3_failed = True
    return _ocr3


def _get_ocr():
    """Legacy fallback: rapidocr_onnxruntime 1.2.3 (PP-OCRv3)."""
    global _ocr
    global OCR_PROVIDER, OCR_ENGINE
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
        _add_torch_dlls()
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
        OCR_ENGINE = "rapidocr_onnxruntime PP-OCRv3"
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


class _GDino:
    """Grounding DINO tiny, fp16 CUDA, fixed text prompt (tokenised once),
    GPU-side preprocessing. Returns [(caption, score, x1, y1, x2, y2)]."""

    def __init__(self):
        import torch
        from transformers import AutoModelForZeroShotObjectDetection, AutoTokenizer

        self.torch = torch
        self.dev = "cuda"
        self.model = AutoModelForZeroShotObjectDetection.from_pretrained(
            GDINO_MODEL, dtype=torch.float16).to(self.dev).eval()
        tok = AutoTokenizer.from_pretrained(GDINO_MODEL)
        phrases = list(GDINO_VOCAB)
        text = " . ".join(phrases) + " ."
        self.tin = tok([text], return_tensors="pt").to(self.dev)
        ids = self.tin["input_ids"][0].tolist()
        dot = tok.convert_tokens_to_ids(".")
        # token index spans of each phrase in the prompt; a box's label is the
        # phrase with the highest token score (avoids merged "stamp loudspeaker")
        spans, cur = [], []
        for i, t in enumerate(ids[1:-1], start=1):
            if t == dot:
                spans.append(cur)
                cur = []
            else:
                cur.append(i)
        self.phrases = phrases
        self.spans = spans[: len(phrases)]
        self.tin_doc = tok([" . ".join(COUNTER_QUERIES) + " ."], return_tensors="pt").to(self.dev)
        self.mean = torch.tensor([0.485, 0.456, 0.406], device=self.dev).view(1, 3, 1, 1)
        self.std = torch.tensor([0.229, 0.224, 0.225], device=self.dev).view(1, 3, 1, 1)

    def __call__(self, frame: np.ndarray) -> list[tuple]:
        torch = self.torch
        H, W = frame.shape[:2]
        h = GDINO_SHORT
        w = int(round(W * h / H / 32) * 32)
        x = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
        x = torch.from_numpy(x[..., ::-1].copy()).to(self.dev).permute(2, 0, 1)[None].half() / 255
        x = ((x - self.mean) / self.std).half()
        with torch.inference_mode():
            out = self.model(pixel_values=x, pixel_mask=torch.ones((1, h, w), dtype=torch.long, device=self.dev),
                             **self.tin)
            prob = out.logits[0].float().sigmoid()              # (Q, T)
            ps = torch.stack([prob[:, s].max(-1).values for s in self.spans], -1)  # (Q, P)
            score, which = ps.max(-1)
            bx = out.pred_boxes[0].float()
            keep = score > min(v[1] for v in GDINO_VOCAB.values())
            score, which, bx = score[keep].cpu().numpy(), which[keep].cpu().numpy(), bx[keep].cpu().numpy()
        dets = []
        for sc, k, (cx, cy, bw, bh) in zip(score, which, bx):
            cap, th = GDINO_VOCAB[self.phrases[k]]
            if sc < th or bw * bh > GDINO_MAX_FRAC:
                continue
            dets.append((cap, float(sc), max(0, int((cx - bw / 2) * W)), max(0, int((cy - bh / 2) * H)),
                         min(W - 1, int((cx + bw / 2) * W)), min(H - 1, int((cy + bh / 2) * H))))
        # per-caption NMS
        dets.sort(key=lambda d: -d[1])
        out_d: list[tuple] = []
        for d in dets:
            bd = Box(*d[2:])
            if any(o[0] == d[0] and iou(bd, Box(*o[2:])) > 0.5 for o in out_d):
                continue
            out_d.append(d)
        return out_d


    def counter(self, frame: np.ndarray) -> list[tuple]:
        """Second pass on the counter strip only -> [(COUNTER_CAP, score, x1, y1, x2, y2)]."""
        torch = self.torch
        H, W = frame.shape[:2]
        X1, Y1 = int(COUNTER_STRIP[0] * W), int(COUNTER_STRIP[1] * H)
        X2, Y2 = int(COUNTER_STRIP[2] * W), int(COUNTER_STRIP[3] * H)
        crop = frame[Y1:Y2, X1:X2]
        ch, cw = crop.shape[:2]
        if ch < 32 or cw < 32:
            return []
        h = COUNTER_SHORT
        w = int(round(cw * h / ch / 32) * 32)
        x = cv2.resize(crop, (w, h), interpolation=cv2.INTER_AREA if h < ch else cv2.INTER_NEAREST)
        x = torch.from_numpy(x[..., ::-1].copy()).to(self.dev).permute(2, 0, 1)[None].half() / 255
        x = ((x - self.mean) / self.std).half()
        with torch.inference_mode():
            out = self.model(pixel_values=x, pixel_mask=torch.ones((1, h, w), dtype=torch.long, device=self.dev),
                             **self.tin_doc)
            score = out.logits[0].float().sigmoid().max(-1).values
            bx = out.pred_boxes[0].float()
            keep = score >= COUNTER_DOC_MIN
            score, bx = score[keep].cpu().numpy(), bx[keep].cpu().numpy()
        dets = []
        for sc, (cx, cy, bw, bh) in sorted(zip(score, bx), key=lambda t: -t[0]):
            if bw * bh > COUNTER_DOC_MAX_FRAC or min(bw * cw, bh * ch) < COUNTER_DOC_MIN_PX:
                continue
            d = (COUNTER_CAP, float(sc), X1 + max(0, int((cx - bw / 2) * cw)), Y1 + max(0, int((cy - bh / 2) * ch)),
                 X1 + min(cw - 1, int((cx + bw / 2) * cw)), Y1 + min(ch - 1, int((cy + bh / 2) * ch)))
            if any(iou(Box(*d[2:]), Box(*o[2:])) > 0.3 for o in dets):
                continue
            dets.append(d)
        return dets


class _Clip:
    """CLIP ViT-B/16 zero-shot classifier over CLIP_VOCAB, fp16 CUDA."""

    def __init__(self):
        import torch
        from transformers import CLIPModel, CLIPTokenizer

        self.torch = torch
        self.dev = "cuda"
        self.m = CLIPModel.from_pretrained(CLIP_MODEL, dtype=torch.float16).to(self.dev).eval()
        tok = CLIPTokenizer.from_pretrained(CLIP_MODEL)
        with torch.inference_mode():
            t = tok([f"a pixel art picture of a {v}" for v in CLIP_VOCAB], padding=True,
                    return_tensors="pt").to(self.dev)
            T = self.m.get_text_features(**t)
            T = getattr(T, "pooler_output", T)
            self.T = T / T.norm(dim=-1, keepdim=True)
        self.mean = torch.tensor([0.4815, 0.4578, 0.4082], device=self.dev).view(1, 3, 1, 1)
        self.std = torch.tensor([0.2686, 0.2613, 0.2758], device=self.dev).view(1, 3, 1, 1)

    def __call__(self, frame: np.ndarray, boxes: list[Box]) -> list[tuple[str, float]]:
        if not boxes:
            return []
        torch = self.torch
        H, W = frame.shape[:2]
        crops = []
        for b in boxes:
            p = int(0.08 * max(b.w, b.h))
            x1, y1, x2, y2 = max(0, b.x1 - p), max(0, b.y1 - p), min(W, b.x2 + p), min(H, b.y2 + p)
            c = frame[y1:y2, x1:x2]
            h, w = c.shape[:2]
            s = max(h, w, 1)
            sq = np.zeros((s, s, 3), np.uint8)
            sq[(s - h) // 2:(s - h) // 2 + h, (s - w) // 2:(s - w) // 2 + w] = c
            crops.append(cv2.resize(sq, (224, 224), interpolation=cv2.INTER_AREA))
        x = torch.from_numpy(np.stack(crops)[..., ::-1].copy()).to(self.dev).permute(0, 3, 1, 2).half() / 255
        x = ((x - self.mean) / self.std).half()
        with torch.inference_mode():
            I = self.m.get_image_features(pixel_values=x)
            I = getattr(I, "pooler_output", I)
            I = I / I.norm(dim=-1, keepdim=True)
            P = (100 * I @ self.T.T).float().softmax(-1)
            pv, pi = P.max(-1)
        return [(CLIP_VOCAB[i], float(v)) for v, i in zip(pv.cpu().tolist(), pi.cpu().tolist())]


def _get_labellers():
    global _gdino, _clip, _labels_failed
    if LABELS and _gdino is None and not _labels_failed:
        try:
            import torch

            if not torch.cuda.is_available():
                raise RuntimeError("no CUDA")
            _gdino = _GDino()
            _clip = _Clip()
        except Exception as e:  # labels optional; extract still returns boxes
            print(f"[extract] open-vocab labelling unavailable: {e!r}")
            _labels_failed = True
            _gdino = _clip = None
    return _gdino, _clip


def warmup() -> None:
    dummy = np.zeros((1280, 2280, 3), np.uint8)
    cv2.putText(dummy, "WARMUP", (400, 640), cv2.FONT_HERSHEY_SIMPLEX, 4, (255, 255, 255), 8)
    cv2.rectangle(dummy, (1200, 200), (1700, 900), (200, 200, 180), -1)
    for _ in range(2):  # 2nd pass: cuDNN algo caches warm
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


_KEEP_EXTRA = set("—–‘’“”€£°")


def _clean_ocr(t: str) -> str:
    """Normalise recogniser output: the multilingual PP-OCRv5 dictionary can
    emit CJK glyphs / emoji on pixel-art noise; keep Latin text only."""
    t = t.replace("…", "...").replace("·", " ")
    t = "".join(c for c in t if ord(c) < 0x250 or c in _KEEP_EXTRA)
    t = _clean(t)
    t = re.sub(r"^[.\s:;,]+(?=\w)", "", t)  # leader dots: "......Shutter" -> "Shutter"
    return t if re.search(r"[A-Za-z0-9]", t) else ""


def _lex_frac(t: str) -> float:
    words = [w for w in re.findall(r"[A-Za-z]+", t) if len(w) >= 3]
    if not words or not _lexicon:
        return 0.0
    return sum(w.lower() in _lexicon for w in words) / len(words)


def _ocr_boxes(native: np.ndarray, s: int) -> list[Box]:
    eng = _get_ocr3()
    if eng is None:
        return _ocr_boxes_legacy(native, s)
    H, W = native.shape[:2]
    up = cv2.resize(native, None, fx=OCR_UPSCALE, fy=OCR_UPSCALE, interpolation=cv2.INTER_NEAREST)
    det = eng(up, use_rec=False, use_cls=False)
    polys = det.boxes if det.boxes is not None else []
    rects, crops = [], []
    for poly in polys:
        p = np.asarray(poly, np.float32) / OCR_UPSCALE
        x1, y1 = np.floor(p.min(0)).astype(int)
        x2, y2 = np.ceil(p.max(0)).astype(int)
        x1, y1, x2, y2 = max(0, x1), max(0, y1), min(W, x2), min(H, y2)
        if x2 - x1 < 2 or y2 - y1 < 3:
            continue
        c = native[max(0, y1 - OCR_REC_PAD):y2 + OCR_REC_PAD, max(0, x1 - OCR_REC_PAD):x2 + OCR_REC_PAD]
        crops.append(cv2.resize(c, None, fx=OCR_REC_UPSCALE, fy=OCR_REC_UPSCALE, interpolation=cv2.INTER_LINEAR))
        rects.append((x1, y1, x2, y2))
    if not crops:
        return []
    try:
        rec = eng.recognize_txt(crops)
        reads = [[(_clean_ocr(t), float(c))] for t, c in zip(rec.txts, rec.scores)]
        # second opinion (English recogniser) only for short lines -- labels and
        # buttons -- whose first reading contains a non-dictionary word: the
        # pixel font's N/H, A/R, V/U, E/F glyphs are ambiguous to either model.
        redo = [i for i, r in enumerate(reads) if _ocr3_en is not None and r[0][0]
                and len(r[0][0].split()) <= OCR_SECOND_MAX_WORDS and _lex_frac(r[0][0]) < 1.0]
        if redo:
            rec2 = _ocr3_en.recognize_txt([crops[i] for i in redo])
            for i, t, c in zip(redo, rec2.txts, rec2.scores):
                reads[i].append((_clean_ocr(t), float(c)))
    except Exception:
        return []
    out: list[Box] = []
    for i, (x1, y1, x2, y2) in enumerate(rects):
        # best score + dictionary-word bonus
        txt, conf = max(reads[i], key=lambda tc: tc[1] + OCR_LEX_BONUS * _lex_frac(tc[0]))
        if conf < OCR_MIN_CONF or not txt:
            continue
        out.append(Box(int(x1) * s, int(y1) * s, int(x2) * s, int(y2) * s, txt, "text", conf))
    return out


def _ocr_boxes_legacy(native: np.ndarray, s: int) -> list[Box]:
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


def _ocr_timed(native: np.ndarray, s: int) -> tuple[list[Box], float]:
    t = time.perf_counter()
    r = _ocr_boxes(native, s)
    return r, (time.perf_counter() - t) * 1e3


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


# --------------------------------------------------------------------------
# labels (captions)
# --------------------------------------------------------------------------


def _det_matches(b: Box, d: Box) -> bool:
    """Does detector box d describe candidate box b?"""
    if iou(b, d) >= 0.5:
        return True
    if _contain_frac(b, d) >= 0.8 and b.area >= 0.3 * d.area:   # b is most of d
        return True
    return _contain_frac(d, b) >= 0.8 and d.area >= 0.5 * b.area  # d is most of b


def _apply_dets(boxes: list[Box], dets: list[tuple], score: dict[int, float]) -> list[tuple]:
    """Caption every box a detection covers; return the detections that
    matched nothing."""
    left = []
    for cap, sc, *xy in dets:
        d = Box(*xy)
        hit = False
        for b in boxes:
            if b.kind == "page_corner" or not _det_matches(b, d):
                continue
            hit = True
            if sc > score.get(id(b), 0.0):
                b.caption, score[id(b)] = cap, sc
        if not hit:
            left.append((cap, sc, *xy))
    return left


def _resolve_parts(dets: list[tuple]) -> list[tuple]:
    """A detection lying inside a bigger detection with another label is a part
    of it (GDINO calls the stamp knobs 'loudspeaker' inside the 'rubber stamp'
    bar): it takes the container's label."""
    out = []
    for d in dets:
        bd = Box(*d[2:])
        if d[0] == COUNTER_CAP:
            out.append(d)
            continue
        cont = [e for e in dets if e is not d and e[0] != d[0] and e[0] not in ("folded page corner", COUNTER_CAP)
                and Box(*e[2:]).area > 1.6 * bd.area and _contain_frac(bd, Box(*e[2:])) >= 0.85]
        if cont and d[0] != "folded page corner":
            c = max(cont, key=lambda e: e[1])
            d = (c[0], d[1], *d[2:])
        out.append(d)
    return out


def _clip_caption(frame: np.ndarray, boxes: list[Box], clip) -> None:
    if clip is None or not boxes:
        return
    for b, (lab, p) in zip(boxes, clip(frame, boxes)):
        b.caption = lab if p >= (CLIP_DOC_P if lab in DOC_CAPS else CLIP_MIN_P) else f"possibly {lab}"


EDGE_TAB_CAP = "tab at screen edge"


def _edge_tabs(native: np.ndarray, s: int) -> list[Box]:
    """Narrow tall tabs sticking out of the left/right frame edge (the stamp
    tray handle at the desk's right edge). The panel finder misses them: they
    are open on the frame side and smaller than its minimum area. Contours on
    the native frame padded with a 1 px black border; kept if they touch a side
    edge, are <= 3% of the width, >= 4x as tall as wide, boxy and contrasting."""
    H, W = native.shape[:2]
    gray = cv2.cvtColor(native, cv2.COLOR_BGR2GRAY)
    e = cv2.Canny(cv2.copyMakeBorder(gray, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0), 30, 90)
    e = cv2.dilate(e, np.ones((2, 2), np.uint8))
    cnts, _ = cv2.findContours(e, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    out: list[Box] = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        x, y = x - 1, y - 1
        if not (x <= 1 or x + w >= W - 1) or w > 0.03 * W or h < 4 * w or w < 4 or h > 0.5 * H:
            continue
        if cv2.contourArea(cv2.convexHull(c)) / max(1, w * h) < 0.6:
            continue
        x1, y1, x2, y2 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
        m = max(2, w // 2)
        ring = gray[max(0, y1 - m):min(H, y2 + m), max(0, x1 - m):min(W, x2 + m)].astype(np.float32)
        inner = gray[y1:y2, x1:x2].astype(np.float32)
        rn = ring.size - inner.size
        if rn <= 0 or abs(float(inner.mean()) - (ring.sum() - inner.sum()) / rn) < 18:
            continue
        b = Box(x1 * s, y1 * s, x2 * s, y2 * s, kind="object", conf=0.5, caption=EDGE_TAB_CAP)
        if not any(iou(b, o) > 0.5 for o in out):
            out.append(b)
    return out


def _counter_docs(boxes: list[Box]) -> None:
    """A 'document on counter' / edge-tab box replaces the untexted
    icon/panel/object boxes that are mostly the same thing (in place)."""
    own = (COUNTER_CAP, EDGE_TAB_CAP)
    docs = [b for b in boxes if b.caption in own and b.kind == "object"]
    for d in docs:
        for b in list(boxes):
            if b is d or b.text or b.kind == "page_corner" or b.caption in own:
                continue
            if iou(b, d) > 0.5 or (_contain_frac(b, d) > 0.8 and b.area > 0.3 * d.area):
                boxes.remove(b)


CONTROL_CAPS = {"speaker/horn", "lever handle"}


def _drop_printed_controls(boxes: list[Box], frame_area: int) -> None:
    """Controls do not lie on paper: a speaker/lever detection inside a texted
    sheet (the rulebook diagram, the bulletin's tape) is a drawing -> dropped."""
    sheets = [b for b in boxes if b.kind == "panel" and b.text and b.area >= PAPER_MIN_FRAC * frame_area]
    for b in list(boxes):
        if b.caption in CONTROL_CAPS and not b.text and any(
                S.area > 4 * b.area and _contain_frac(b, S) >= 0.9 for S in sheets):
            boxes.remove(b)


_CLIP_RECHECK = {"passport booklet", "paper document"}


def _clip_docs(frame: np.ndarray, boxes: list[Box], clip) -> None:
    """Second opinion on paper-like boxes: Grounding DINO's 'passport' /
    'document' fire on the bulletin, the rulebook and a strip of tape alike.
    CLIP names a passport / rulebook (DOC_CAPS) with p >= CLIP_DOC_P -> that
    label wins; a 'passport booklet' CLIP does not confirm becomes a plain
    'paper document'."""
    if clip is None:
        return
    cand = [b for b in boxes if b.caption in _CLIP_RECHECK or (b.text and b.caption in DOC_CAPS)
            or (b.kind == "panel" and b.text and b.area >= PAPER_MIN_FRAC * frame.shape[0] * frame.shape[1])]
    if not cand:
        return
    for b, (lab, p) in zip(cand, clip(frame, cand)):
        if b.text:
            # texted sheets: the bulletin reads 'closed passport' up to 0.63,
            # the open passport 'open passport' 0.72 -> only that, only high
            if lab == "open passport" and p >= CLIP_OPEN_TEXT_P:
                b.caption = lab
            elif b.caption in DOC_CAPS:   # crop label from before the text was attached
                b.caption = ""
        elif lab in DOC_CAPS and p >= CLIP_DOC_P:
            b.caption = lab
        elif b.caption == "passport booklet":
            b.caption = "paper document"


def _corners(out: list[Box], dets: list[tuple], frame_area: int) -> list[Box]:
    """Page corners. Grounding DINO localises whole papers well ('folded page
    corner' / 'passport' / 'document' all fire on the full sheet, 0.25-0.6)
    but not the corner itself. So: each detected paper gets a page_corner at
    its bottom-right, and the synthetic corners _merge proposed for panel
    fragments inside that paper are dropped. Synthetic corners of papers the
    detector missed stay as a fallback."""
    papers: list[tuple[Box, float, bool]] = []
    for cap, sc, *xy in sorted(dets, key=lambda d: -d[1]):
        D = Box(*xy)
        folded = cap == "folded page corner"
        if cap not in PAPER_CAPS:
            continue
        if D.area < PAPER_MIN_FRAC * frame_area:
            if folded and sc >= 0.3 and max(D.w, D.h) < 200:   # the corner itself
                out.append(Box(*xy, kind="page_corner", conf=round(sc, 3), caption="folded page corner"))
            continue
        for i, (P, psc, pf) in enumerate(papers):
            if iou(P, D) > 0.6:
                papers[i] = (P, psc, pf or folded)
                break
        else:
            if folded and sc < 0.25:
                continue
            papers.append((D, sc, folded))
    for D, sc, folded in papers:
        c = max(CORNER_MIN_PX, int(0.12 * min(D.w, D.h)))
        cb = Box(D.x2 - c, D.y2 - c, D.x2, D.y2, kind="page_corner", conf=round(sc, 3),
                 caption="folded page corner" if folded else "page corner")
        inside = [b for b in out if b.kind == "panel" and b.text and _contain_frac(b, D) >= 0.8]
        cb.parent = max(inside, key=lambda b: b.area).text if inside else ""
        out = [b for b in out if not (b.kind == "page_corner" and not b.caption
                                      and (_contain_frac(b, D) >= 0.8 or iou(b, cb) > 0.3))]
        if any(b.kind == "page_corner" and iou(b, cb) > 0.3 for b in out):
            continue
        out.append(cb)
        if not any(iou(b, D) > 0.5 for b in out if b.kind != "page_corner"):
            out.append(Box(D.x1, D.y1, D.x2, D.y2, kind="object", conf=round(sc, 3), caption="paper document"))
    for b in out:
        if b.kind == "page_corner" and not b.caption:
            b.caption = "page corner (proposed, bottom-right of paper)"
    return out


def _dedup(boxes: list[Box]) -> list[Box]:
    """Drop a box fully inside another box with the same text (or, if untexted,
    the same caption). page_corner boxes are always kept."""
    def key(b: Box):
        return ("t", b.text.lower()) if b.text else (("c", b.caption) if b.caption else None)

    order = sorted(boxes, key=lambda b: b.area, reverse=True)
    out: list[Box] = []
    for b in order:
        k = key(b)
        if b.kind != "page_corner" and k is not None and any(
                o.kind != "page_corner" and key(o) == k and _contain_frac(b, o) >= 0.95 for o in out):
            continue
        out.append(b)
    return [b for b in boxes if any(b is o for o in out)]


# --------------------------------------------------------------------------
# description
# --------------------------------------------------------------------------


def coarse_pos(b: Box, W: int, H: int) -> str:
    cx, cy = b.center
    v = "top" if cy < H / 3 else ("bottom" if cy > 2 * H / 3 else "middle")
    h = "left" if cx < W / 3 else ("right" if cx > 2 * W / 3 else "centre")
    return "centre" if (v, h) == ("middle", "centre") else f"{v}-{h}"


def describe(box: Box, W: int | None = None, H: int | None = None) -> str:
    """"<kind> — '<ocr text>'" or "<kind> — <caption>", plus a coarse position
    (frame size defaults to the last frame passed to extract()). Purely what is
    visible: no hints about which element to use."""
    if box.text and box.kind != "page_corner":
        t = box.text if len(box.text) <= 60 else box.text[:57] + "..."
        d = f"{box.kind} — {box.caption}: '{t}'" if box.caption in DOC_CAPS else f"{box.kind} — '{t}'"
    else:
        d = f"{box.kind} — {box.caption or 'unlabelled graphic'}"
    W = W or _LAST_WH[0]
    H = H or _LAST_WH[1]
    return f"{d} ({coarse_pos(box, W, H)})" if W and H else d


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------


def extract(frame_bgr: np.ndarray) -> list[Box]:
    global _LAST_WH
    T: dict[str, float] = {}
    t0 = time.perf_counter()
    s = _native_scale(frame_bgr)
    H, W = frame_bgr.shape[:2]
    _LAST_WH = (W, H)
    if s > 1:
        # pixel art is replicated s x s: sampling one pixel per cell is lossless
        native = np.ascontiguousarray(frame_bgr[: (H // s) * s: s, : (W // s) * s: s])
    else:
        native = frame_bgr
    fut = _pool.submit(_ocr_timed, native, s)          # CPU, overlapped with the GPU work below

    def lap(name: str, t: float) -> float:
        n = time.perf_counter()
        T[name] = (n - t) * 1e3
        return n

    t = time.perf_counter()
    icons = _icon_boxes(frame_bgr)
    t = lap("icon_ms", t)
    gdino, clip = _get_labellers()
    dets = _resolve_parts(gdino(frame_bgr)) if gdino is not None else []
    if gdino is not None and COUNTER_PASS:
        cdets = gdino.counter(frame_bgr)
        # what lies on the counter is the document, not whatever the full-frame
        # pass thought it was ('rubber stamp' 0.25 on the closed passport)
        dets = [d for d in dets if not any(_det_matches(Box(*d[2:]), Box(*c[2:])) for c in cdets)] + cdets
    t = lap("gdino_ms", t)
    panels = _panel_boxes(native, s)
    t = lap("panel_ms", t)
    # caption untexted candidates now, while OCR is still running
    score: dict[int, float] = {}
    obj_dets = [d for d in dets if d[0] != "folded page corner"]
    _apply_dets(icons + panels, obj_dets, score)
    _clip_caption(frame_bgr, [b for b in icons + panels if not b.caption], clip)
    t = lap("clip_ms", t)
    texts, T["ocr_ms"] = fut.result()
    t = lap("ocr_wait_ms", t)

    boxes = _merge(texts, icons, panels, W * H)
    for cap, sc, *xy in _apply_dets(boxes, obj_dets, score):
        boxes.append(Box(*xy, kind="object", conf=round(sc, 3), caption=cap))
    boxes += _edge_tabs(native, s)
    _counter_docs(boxes)
    _drop_printed_controls(boxes, W * H)
    _clip_docs(frame_bgr, boxes, clip)
    t = lap("merge_ms", t)
    boxes = _corners(boxes, dets, W * H)
    boxes = _dedup(boxes)
    # pad text boxes a little so markers/clicks don't sit on the glyph edge
    for b in boxes:
        if b.kind == "text":
            p = max(2, s)
            b.x1, b.y1, b.x2, b.y2 = max(0, b.x1 - p), max(0, b.y1 - p), min(W - 1, b.x2 + p), min(H - 1, b.y2 + p)
    T["merge_ms"] += (time.perf_counter() - t) * 1e3
    t = time.perf_counter()
    T["total_ms"] = (t - t0) * 1e3
    LAST_TIMINGS.clear()
    LAST_TIMINGS.update(T)
    if VERBOSE:
        print("[extract] " + " ".join(f"{k[:-3]}={v:.0f}" for k, v in T.items()) + f" ms  boxes={len(boxes)}")
    return boxes


if __name__ == "__main__":
    import sys

    VERBOSE = True
    warmup()
    for f in sys.argv[1:]:
        img = cv2.imread(f)
        bs = extract(img)
        print(f, {k: round(v) for k, v in LAST_TIMINGS.items()})
        for b in bs:
            print("  ", describe(b), [b.x1, b.y1, b.x2, b.y2])
