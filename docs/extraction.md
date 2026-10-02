# Perception / extraction layer — decision & evidence

Target: screenshot of *Papers, Please* (Unity remaster, pixel-art, custom pixel font,
windowed) -> detect every clickable/draggable UI element as bounding boxes -> OCR the
text inside each -> draw numbered Set-of-Mark markers -> hand box list to a decision API.
Env: Windows 11, Python 3.13.7, RTX 4070 Laptop 8GB, driver 596.08.

## Decision

**Detection: OmniParser-v2 `icon_detect` (YOLOv8, via Ultralytics) on CUDA.**
**OCR: RapidOCR (`rapidocr-onnxruntime`) running on CPU, one det+rec pass over the whole frame.**
**Set-of-Mark: OpenCV.**

OCR is run **once on the whole frame**, not once per detected box: RapidOCR's text
detector produces the tight single-line crops its recogniser needs, so a single
full-frame pass is both faster and far more accurate than feeding it each YOLO box.
Each resulting text line is then attached to the smallest YOLO box whose area contains
the line's centre; OCR lines that fall outside every YOLO box become their own
`label="text"` boxes.

## Why this and not the alternatives

| Candidate | Verdict |
|---|---|
| **OmniParser v2 (full: YOLO + Florence-2 caption + PaddleOCR)** | Rejected as a whole. The Florence-2 captioner adds ~1s/frame and the stock pipeline uses PaddleOCR, whose `paddlepaddle` has no reliable cp313 Windows wheel. But its **`icon_detect` YOLOv8 is excellent and installs cleanly**, so we use just that part. |
| **Grounding DINO / YOLO-World (open-vocab)** | Rejected. Text-prompt open-vocab detection is overkill/slow for "find the clickable panels" and tuning prompts for pixel-art UI is fiddly. |
| **Florence-2 alone (OD + `<OCR_WITH_REGION>`)** | Rejected. Heavy (~1s), its OD targets natural images, and region-OCR on a stylised pixel font was not worth the latency. |
| **PaddleOCR** | Rejected: `paddlepaddle` lags on Python 3.13 / Windows (no dependable wheel). |
| **EasyOCR (GPU, torch)** | Installs on 3.13, runs on GPU, but measured **~1.3s** and **less accurate** on the pixel font (`APPROVE`->`APPROVZ`, `DENY`->`DZNY`, `RULE`->`RUIZ`). Kept as documented fallback only. |
| **Tesseract/pytesseract** | Needs an external binary (not pip); not installed. Viable but avoided to keep the stack pip-only. |
| **RapidOCR (onnxruntime)** | **Chosen.** Pure pip, cp313 wheels, no binary, no paddle. Best pixel-font accuracy of everything tested. |

## OCR runs on CPU, not GPU — important

onnxruntime GPU acceleration was tried and **rejected for OCR**:
- `onnxruntime-gpu` **1.23+ requires CUDA 13**; torch cu126 bundles CUDA 12, so the
  default `onnxruntime-gpu` (1.30) fails to load (`cublasLt64_13.dll missing`).
- Pinning `onnxruntime-gpu==1.22.0` (a CUDA-12 build) **does** load the
  `CUDAExecutionProvider` when torch's `lib/` is added to the DLL search path
  (`os.add_dll_directory(os.path.join(os.path.dirname(torch.__file__),'lib'))`), and raw
  rec inference drops to ~9ms — but the **full RapidOCR pass was consistently slower on
  GPU than CPU** (~1.4s vs ~0.7s) because the PP-OCRv3 det/rec models have dynamic input
  shapes that defeat cuDNN plan caching. So: **keep OCR on plain `onnxruntime` (CPU).**

YOLO detection, by contrast, benefits clearly from CUDA and stays on GPU.

## Exact install commands that worked

```
py -3.13 -m venv .\.venv-extract
.\.venv-extract\Scripts\python -m pip install -U pip
# CUDA torch (cu126) — verified torch.cuda.is_available() == True on the RTX 4070:
... pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126
#   -> torch 2.14.1+cu126, torchvision 0.29.1+cu126
... pip install ultralytics rapidocr-onnxruntime opencv-python pillow
# NOTE: EasyOCR (optional) pulls opencv-python-headless and conflicts with opencv-python
#       on a locked cv2.pyd; if you install easyocr, uninstall opencv-python first and
#       let headless win (cv2 API we use works fine headless).
```

Pinned set in `requirements-extract.txt`.

## Weight paths

- YOLO detector: `.\models\icon_detect_model.pt`
  (40.6 MB, from `microsoft/OmniParser-v2.0`, file `icon_detect/model.pt`; downloaded via
  `https://huggingface.co/microsoft/OmniParser-v2.0/resolve/main/icon_detect/model.pt`).
- RapidOCR weights ship inside the `rapidocr-onnxruntime` wheel (PP-OCRv3 det/rec +
  mobile cls ONNX) — no separate download.
- EasyOCR (if used) weights downloaded to `.\models\easyocr\`.

## Minimal working `extract()` (verified to run)

```python
import time
from dataclasses import dataclass
import cv2, numpy as np
from ultralytics import YOLO
from rapidocr_onnxruntime import RapidOCR

MODEL_PATH = r".\models\icon_detect_model.pt"
DET_CONF, IMGSZ = 0.1, 640

@dataclass
class Box:
    x1: int; y1: int; x2: int; y2: int
    label: str; text: str; conf: float

_yolo = YOLO(MODEL_PATH)
_ocr = RapidOCR(**{"Global.use_angle_cls": False,
                   "Det.limit_type": "max", "Det.limit_side_len": 960})  # CPU

def _center_in(b, pt):
    return b[0] <= pt[0] <= b[2] and b[1] <= pt[1] <= b[3]

def extract(frame_bgr):
    H, W = frame_bgr.shape[:2]
    r = _yolo.predict(frame_bgr, imgsz=IMGSZ, conf=DET_CONF, device=0, verbose=False)[0]
    boxes = [Box(max(0, int(b.xyxy[0][0])), max(0, int(b.xyxy[0][1])),
                 min(W, int(b.xyxy[0][2])), min(H, int(b.xyxy[0][3])),
                 "icon", "", float(b.conf[0])) for b in r.boxes]

    res, _ = _ocr(frame_bgr)                      # one full-frame OCR pass
    per_box = {i: [] for i in range(len(boxes))}
    for quad, txt, score in (res or []):
        if not txt.strip():
            continue
        xs = [p[0] for p in quad]; ys = [p[1] for p in quad]
        cx, cy = sum(xs) / 4.0, sum(ys) / 4.0
        best, best_area = None, None
        for i, bx in enumerate(boxes):
            if _center_in((bx.x1, bx.y1, bx.x2, bx.y2), (cx, cy)):
                a = (bx.x2 - bx.x1) * (bx.y2 - bx.y1)
                if best_area is None or a < best_area:
                    best, best_area = i, a
        if best is not None:
            per_box[best].append((cy, cx, txt))
        else:
            boxes.append(Box(int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)),
                             "text", txt.strip(), float(score)))
    for i, items in per_box.items():
        items.sort()
        boxes[i].text = " ".join(t for _, _, t in items).strip()
    return boxes
```

Set-of-Mark drawing (OpenCV: green rect for `icon`, orange for `text`, red numbered tag
at each box's top-left) is in the reference `extract.py`; it adds ~1ms.

### Verified output on the synthetic pixel-art test frame (640x400)

```
boxes=6
 [0] icon (516,89,614,222) conf=0.94 text='STAMP'
 [1] icon (358,88,502,142) conf=0.90 text='APPROVE'
 [2] icon (358,167,502,222) conf=0.89 text='DENY'
 [3] icon (358,258,412,302) conf=0.87 text='RULE'
 [4] icon (427,258,482,302) conf=0.84 text='CLK'
 [5] icon (30,59,301,304) conf=0.82
        text='ARSTOTZKA NAME : KOWALSKI, DOB: 1961.04.22 ID+: GC07D-3821 ENTRY PERMIT'
```

All 6 clickable regions detected; every button label read correctly; the multi-line
passport card read almost perfectly (only `ID#` -> `ID+`).

## Measured latency (RTX 4070 Laptop, synthetic 640x400 frame)

| Stage | Time |
|---|---|
| YOLO `icon_detect` detection (GPU) | **~40 ms** (clean); 70–110 ms under load |
| RapidOCR full-frame det+rec (CPU) | **~0.7 s** best clean reading (700–780 ms) |
| Set-of-Mark draw | ~1 ms |
| **Whole pass** | **~0.75–0.85 s** |

**The ~500 ms target is NOT met by the accurate full-OCR path (~0.75 s).** The OCR pass
is the entire bottleneck; detection + SoM alone is ~40 ms and sits far under budget.
Note measurements in this build environment were noisy (OCR swung 0.7 s–2.7 s) because a
sandbox infrastructure process intermittently consumed ~80% CPU; the ~0.7 s figure is the
cleanest standalone reading and is the realistic unloaded number.

### Options if you must hit <500 ms
- **Accept ~0.75 s.** Papers, Please is not a twitch game; a sub-second decision loop is
  usually fine. Recommended default.
- **OCR a region of interest, not the whole frame.** If the decision only needs, e.g.,
  the passport + the approve/deny buttons, crop to that ROI before the OCR pass — det
  cost scales with input area.
- **Direct-recogniser path (measured ~25–45 ms):** skip RapidOCR's detector and batch the
  recogniser (`_ocr.text_recognizer([crops])`) over the YOLO boxes. This hits the latency
  target with huge margin but **accuracy collapses** unless each crop is a tight,
  single-line, correctly-binarised text strip — a naive Otsu tight-crop over coloured
  buttons read only 1/6 regions. Would need a solid per-box text-localisation step first.
- `onnxruntime-gpu==1.22.0` does **not** help (slower on these dynamic-shape models).

## Known weaknesses

- **Latency:** accurate path ~0.75 s, over the 500 ms goal (see above).
- **Small pixel body text:** large labels (>=~14px) read perfectly; very small (~10px)
  body text can still garble some glyphs. At **native resolution** RapidOCR did best —
  pre-upscaling 2–3x before the *full* pipeline actually *hurt* (its detector already
  rescales internally), though upscaling helps only on the direct-recogniser path.
- **GPU OCR is a trap here:** onnxruntime CUDA needs a CUDA-12 build (`==1.22.0`) plus
  torch's DLLs on the path, and is still slower than CPU. Don't bother.
- **8GB VRAM:** YOLO `icon_detect` is tiny (~40MB) so VRAM is a non-issue for this stack;
  it would matter only if the Florence-2 captioner were added (it is not).
- **Box->text mapping** is centre-in-box; a text line spanning two overlapping boxes is
  assigned to the smaller one only. Fine for Papers, Please's non-overlapping panels.
- **Tested on a synthetic PIL frame**, not the real game (game need not be running).
  Real frames may need the detector `conf` and `Det.limit_side_len` retuned, and a
  windowed-capture crop to the game client area.
- **EasyOCR/opencv install conflict:** installing EasyOCR after opencv-python can fail on
  a locked `cv2.pyd`; use opencv-python-headless.
```
