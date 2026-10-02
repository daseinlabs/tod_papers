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


---

# Update 2026-10-01 (evening): perception-quality pass on real game frames

The sections above were written against a synthetic 640x400 frame. This pass worked on
real 2280x1280 captures (`runs/*/raw_*.png`, `captures/*.png`) and changed
`src/tod_papers/extract.py` as follows. The public API is unchanged:
`extract(frame_bgr) -> list[Box]`. `Box` gains `caption: str`, and there is a new
`describe(box, W=None, H=None) -> str`.

## Current pipeline

| Stage | Where | What |
|---|---|---|
| native frame | CPU | `frame[::4, ::4]`. The game renders 570x320 art at exactly 4x (every 4x4 cell is one colour, so sampling is lossless) |
| OCR (worker thread, overlapped with the GPU stages) | CPU onnxruntime | RapidOCR **3.9.2**. PP-OCRv4 mobile **det** runs on a 2x nearest-neighbour copy of the native frame. Each line is re-cropped from the **native** frame with 2 px padding, upscaled **3x bilinear**, and recognised by PP-OCRv5 mobile **rec**. Short lines (<= 4 words) with a non-dictionary word get a second read from **PP-OCRv4 English rec**. The reading with the best `score + 0.15 x dictionary-word fraction` wins (dictionary = BERT uncased `vocab.txt` shipped with grounding-dino-tiny). CJK and emoji output is stripped. |
| icons | CUDA | OmniParser v2 `icon_detect` YOLO (unchanged) |
| open-vocab labels | CUDA fp16 | **Grounding DINO tiny**, whole frame at short side 640, fixed prompt (see `GDINO_VOCAB`) |
| panels | CPU | contour proposals (unchanged) |
| crop labels | CUDA fp16 | **CLIP ViT-B/16** zero-shot over `CLIP_VOCAB`, run only on icon/panel boxes that Grounding DINO did not label |
| merge | CPU | Unchanged merge, then: GDINO detections caption the boxes they cover (IoU >= 0.5 or mutual containment). Unmatched detections become `kind="object"` boxes. Detected papers produce `page_corner` boxes. Dedup step. |

Grounding DINO details:

- **Part-of rule.** A detection inside a bigger detection with a different label takes the container's label. GDINO calls the stamp knobs "loudspeaker" (0.45) inside the "rubber stamp" bar.
- **Paper-to-corner rule.** "folded page corner", "passport" and "document" all fire on the *whole sheet* (0.25–0.6), never on the corner itself. So each detected paper (>= 3% of the frame) gets a `page_corner` at its bottom-right, captioned "folded page corner" or "page corner". The synthetic corners that `_merge` proposed for panel fragments inside that paper are dropped. Synthetic corners for papers the detector missed remain as a fallback, captioned "page corner (proposed, bottom-right of paper)".
- **Prompt choices.**
  - "lever handle" hits a fence post. **"yellow lever"** hits the shutter lever (0.39–0.49).
  - "loudspeaker" plus "horn speaker" are both mapped to the caption `speaker/horn`.
  - Detections larger than 20% of the frame are ignored; the outside wall otherwise comes back as "bulletin board".

Dedup: a box fully inside (>= 95%) another box with the same text (or, if untexted, the same caption) is dropped. `page_corner` boxes are always kept.

`describe(box)` returns `"<kind> — '<ocr text>'"` or `"<kind> — <caption>"`, followed by
` (top-left|top-centre|…|centre|…|bottom-right)`. The frame size defaults to the last
frame passed to `extract()`. It describes only what is visible and gives no hint about
which element to use. Note: `loop.py` already appends its own coarse position after
`ex.describe(b)`, so the loop currently prints the position twice. The fix belongs to
loop.py's owner: either drop `coarse_pos` there or call `describe(b, W, H)`.

Environment flags:

| Flag | Default | Effect |
|---|---|---|
| `TOD_OCR_GPU=1` | off | Opt-in CUDA EP for OCR. It was slower, so CPU stays the default. |
| `TOD_OCR_THREADS` | 8 | onnxruntime intra-op threads |
| `TOD_OCR_SECOND=0` | on | Disables the English second-opinion recogniser |
| `TOD_LABELS=0` | on | Disables GDINO and CLIP. Boxes then have no caption. |
| `TOD_GDINO_SIZE` | 640 | Short side fed to GDINO |
| `TOD_EXTRACT_VERBOSE=0` | on | Silences the per-call timing line |

`LAST_TIMINGS` holds `icon_ms, gdino_ms, panel_ms, clip_ms, ocr_ms` (worker thread),
`ocr_wait_ms, merge_ms, total_ms`.

## Model files (all under `models/`)

| Path | Source | Used |
|---|---|---|
| `icon_detect_model.pt` | microsoft/OmniParser-v2.0 `icon_detect/model.pt` | yes |
| `rapidocr/ch_PP-OCRv4_det_mobile.onnx` | auto-downloaded by rapidocr 3.9.2 (modelscope RapidAI/RapidOCR), copied here | yes |
| `rapidocr/ch_PP-OCRv5_rec_mobile.onnx` | same | yes |
| `rapidocr/en_PP-OCRv4_rec_mobile.onnx` | same | yes |
| `grounding-dino-tiny/` (659 MB) | IDEA-Research/grounding-dino-tiny | yes (also supplies `vocab.txt` for the OCR lexicon) |
| `clip-vit-base-patch16/` | openai/clip-vit-base-patch16. The repo only has `pytorch_model.bin`; it was loaded with `CLIPModel` and re-saved with `save_pretrained`, giving `model.safetensors` | yes |
| `florence2-base/` (447 MB) | florence-community/Florence-2-base (native transformers 5 layout) | evaluated, not used |
| `omniparser-v2/icon_caption/` and `omniparser-caption-hf/` | microsoft/OmniParser-v2.0 `icon_caption`, converted with `scripts/convert_omniparser_caption.py` (old remote-code key layout to native `Florence2ForConditionalGeneration`; fp16; 0 unmapped / 0 unfilled keys) | evaluated, not used |
| `owlv2-base-ensemble/` (593 MB) | google/owlv2-base-patch16-ensemble | evaluated, not used |

Install, in .venv-loop:

```
.venv-loop\Scripts\python -m pip install transformers==5.18.0 rapidocr==3.9.2
# rapidocr 3.x downloads its default models into site-packages\rapidocr\models on first use;
# copy the three .onnx files above into models\rapidocr\ (extract.py loads them from there)
# HF snapshots: huggingface_hub.snapshot_download(repo, local_dir="models/<name>")
```

## OCR: before and after (real frames)

"Before" means the previous `extract.py`: rapidocr_onnxruntime 1.2.3 (PP-OCRv3), native frame upscaled 2x nearest-neighbour, one full pass.

| Frame | Before | After |
|---|---|---|
| booth `215501/raw_0003` | `DERED` | `DENIED` |
| | `APPROVED` | `APPROVED` |
| | `ALICH YISA BEHEATH STAMP` | `ALIGN VISA BENEATH STAMP` |
| | `Glory to Arstotzkd.` | `Glory to Arstotzka.` |
| | `…restricted to Arstotzkon citizens only` | `…restricted to Arstotzkan citizens only.` |
| | `Stamp passport EHTRy VIsA and` | `stamp passport ENTRy VIsA and` |
| | `Grestin Bord` / `Checkpd` | `Grestin Bordr Checkpoint.` |
| | `M1.0.4` | `M.O.A` |
| NEXT screen `214249/raw_0004` | `HEXT` | `NEXT` |
| menu `captures/launch_01` | `EHDLESS`, (PAPERS missed) | `ENDLESS`, `PAPERS`, `PLEASE` |
| booth `221705/raw_0030` | `Artotrkan Ministry +f Admissi+n`, `Shutt2r`, `Count2r`, `Timz &: Dat` | `Arrtotzkan.. Minirtry or Admizzion`, `Shutter`, `Counter`, `Time & Date` |
| newspaper `214516/raw_0002` | `Ho42mb2r 23rd, 1982`, `N+ Charga`, `Aftar 6 Long 4ears` | `Hovember 23rd, 1982`, `No Charge`, `Rfter 6 Long Years` |

Still wrong: "Arstotzkan" on the small Inspector's-booth sheet, "Hovember", "Rfter", and "Welcome toy / sition at". The last one is a line partly hidden behind the stamp bar.

Variants tried. No single recogniser or preprocessing got all five targets (ALIGN VISA BENEATH STAMP / APPROVED / DENIED / NEXT / ARSTOTZKA):

- **PP-OCRv3 (1.2.3), any preprocessing** (nearest/area 1x, 2x/3x NN, full 4x frame, Otsu/adaptive binarisation): fails ALIGN, DENIED and NEXT.
- **Native 1x OCR.** The scoping agent's "native 1x reads STAMP/APPROVE/DENY" result came from the synthetic frame. It is **not confirmed on real booth frames**: at 1x the detector finds only 6 of the 15 lines on `215501/raw_0003` and misses DENIED, APPROVED and ALIGN entirely. 1x does read NEXT.
- **PP-OCRv5 mobile.**
  - 3x nearest-neighbour: reads everything except NEXT and ENDLESS (N/H confusion).
  - 3x bilinear: reads everything except APPROVED (`RPPROUED`).
- **PP-OCRv4 English, 3x bilinear:** 7 of 8 key words; reads NEXT as `HEXT`.
- **PP-OCRv6 small:** `HEXT` in every variant.
- **PP-OCRv5 server:** 10–50x slower, and still `HEXT`/`APPROUED`.
- **Chosen: v5 + v4-en + dictionary bonus.** This combination is the only one that reads all five targets plus ENDLESS, PAPERS and PLEASE.
- **Florence-2-base `<OCR>` on padded line crops** read every target correctly, but took 4.7–5 s for 19 crops on this GPU, so it was rejected.

## Labels: Florence captions vs OWLv2 vs Grounding DINO (booth frames)

| Element (booth `215501/raw_0003`, `221705/raw_0030`) | Florence-2-base `<CAPTION>` per crop | OmniParser icon_caption | OWLv2 (full frame) | **Grounding DINO tiny (full frame)** | CLIP crop |
|---|---|---|---|---|---|
| bulletin page corner (bottom-right) | "a black and white photo of a man in a suit and tie" | "A simple math problem" | only low-score boxes in the wrong places | finds the **whole sheet** as "folded page corner" (0.29) / "passport" (0.38). Corner placed at its bottom-right | n/a |
| shutter lever (yellow, left) | generic photo caption | "A step ladder" | not detected | "yellow lever" 0.39–0.49. ("lever handle" hits a fence post) | "possibly lever handle" on fragments |
| speaker horn on booth roof | generic | "unanswerable" | "speaker horn" fired on the stamp knobs instead | "loudspeaker" 0.22–0.35 on 221705 / 214516; **missed on 215501** (0.18) | "possibly tray" |
| DENIED / APPROVED stamps | "A red box with the word denied on it" | – | stamp knobs labelled "speaker horn" | "rubber stamp" 0.30 (knobs relabelled via the part-of rule) | – |
| clock | – | – | found | 0.26–0.39 | "clock" |
| entrant at window | – | – | – | "person face" 0.45 | "person face" |
| rulebook on desk | – | – | – | "passport" 0.30 | "passport booklet" |
| latency | ~37 s for 59 crops (vision tower ~1.8 s per 8x768², sdpa math fallback) | similar | 560–1150 ms fp16 | **250–370 ms fp16 @640 (quiet GPU)**, 750–1150 ms (contended) | 90–250 ms quiet, 450–650 ms contended (~40 crops) |

Conclusion: Florence-2 captioning (base or OmniParser fine-tune) is unusable on this pixel
art and far over budget. Grounding DINO is the only model that puts correct labels on the
lever, horn, stamps, clock, entrant and papers. CLIP fills in the remaining crops; its
labels on small YOLO fragments of the outside scenery are often noise ("pole", "road
barrier", "door"). It also keeps a CLIP-only label prefixed "possibly" when top-1
probability is below 0.25.

## Measured latency (real 2280x1280 booth frames, warm)

| Condition | icon | gdino | clip | ocr (thread) | **total** |
|---|---|---|---|---|---|
| quiet machine (earlier in the session) | 40–48 ms | 250–370 ms | 170–230 ms | 410–810 ms | **515–815 ms** |
| contended: game + live loop + other agents running; GPU P5 at 1050 MHz | 170–220 ms | 1.1–1.2 s | 0.55–0.65 s | 2.1–3.5 s | **2.2–3.5 s** |
| old extract.py, same contended run | 200–250 ms | – | – | 1.4–4.8 s | 1.6–5.0 s |

The OCR worker is the critical path. GPU work (YOLO → GDINO → CLIP) overlaps it. The
≤ 1.0 s target holds on a quiet machine and not under contention. When contended, the new
pipeline costs about as much as the old OCR-only one did. To trim further:

- `TOD_OCR_SECOND=0` saves about 30% of OCR, at the cost of NEXT/APPROVED-type ambiguities.
- `TOD_LABELS=0` drops GDINO and CLIP.
- `TOD_GDINO_SIZE=512` makes GDINO faster but gives lower lever scores.

Test dumps (described criteria lists, SoM images, before/after JSON) for menu,
newspaper, NEXT and two booth frames are in the session scratchpad `percept/`
(`criteria_*.txt`, `som_*.jpg`, `percept_summary.json`).
