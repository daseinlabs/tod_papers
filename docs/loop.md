# Agent loop: extract → Set-of-Mark → TOD → click

Modules (`src/tod_papers/`):

| file | role |
|---|---|
| `extract.py` | `extract(frame_bgr) -> list[Box]`. Box = `(x1,y1,x2,y2,text,kind,conf,parent)`, client-relative physical px. |
| `som.py` | `annotate(frame, boxes, max_marks=60) -> (annotated, {id: Box})`. Numbered tags + outlines, ids in reading order. |
| `loop.py` | The agent. One TOD request per tick; executes the pick via `io_win.click/drag` at the box centre. |
| `overlay.py` | Writes `viz_NNNN.png`: source-probability heatmap (red fill) + target probability (blue outline) + legend. |

## Running

```
cd .
.venv-loop\Scripts\python.exe -m tod_papers.loop --max-ticks 10
.venv-loop\Scripts\python.exe -m tod_papers.loop --dry-run --max-ticks 3   # no input at all
```

Flags: `--max-ticks N`, `--dry-run`, `--max-marks 60`, `--send-width 1140` (annotated frame is
downscaled to this before upload), `--settle 0.12` (s between hover / down / up so Unity sees them on
separate frames), `--post-wait 0.8`, `--diff-thresh 1.0`, `--save-raw`, `--no-viz`.
Env: `TOD_OCR_THREADS` (default 4), `TOD_OCR_GPU=1` to opt into the onnxruntime CUDA EP (slower here,
see below). TOD key comes from `TOD_API_KEY` / `.env` via `tod_client.py`.

The game must be running windowed (UnityWndClass, title `PapersPlease`); launch e.g.
`PapersPlease.exe -screen-fullscreen 0 -screen-width 2280 -screen-height 1280 -popupwindow`.

## venv

`.venv-loop` is a thin venv layered on `.venv-extract` through a `.pth` file (torch/ultralytics/
rapidocr/opencv come from there) plus `pywin32 dxcam mss comtypes`. Recipe in `requirements-loop.txt`.

## Per tick

1. `focus(hwnd)` and verify `GetForegroundWindow()==hwnd` (Desktop Duplication captures the screen
   region, so an occluded game = wrong frame). Abort (exit 2) if it is not the game.
2. Park the cursor at the client's right edge (it otherwise occludes text, e.g. STORY -> "STRY").
3. Grab → `extract` → `annotate`. An extra id is always appended: "no element - click an empty part
   of the screen" (frame centre) so text/cutscene screens without buttons can be advanced.
4. One TOD call, image = annotated frame, text = objective + rules + last 5 actions, questions:
   `action` {click,drag,wait}, `source` (box ids), `target` (box ids + none), `screen`
   {menu, cutscene_or_text, bulletin, booth_idle, documents_on_desk, stamp_tray_open, inspect_mode,
   day_end, other}, `day` {1,2,3,later,unknown}. Box descriptions: `"<kind> - '<ocr text>' (<coarse
   position>)"`. Day rules (docs/game.md days 1–3) are injected once `day` is answered with p≥0.5.
5. Execute at box centres (bounds-checked against the client rect, foreground re-verified).
   Click = move, 120 ms, down, 120 ms, up. Drag = `io_win.drag` 20 steps / 350 ms.
6. Verify: re-grab after 0.8 s, mean abs grey diff (1/8 scale). "changed"/"NO visible effect" is
   written into the history TOD sees next tick.
7. Log `runs/<ts>/tick_NNNN.png` (annotated), `tick_NNNN.json` (boxes, descriptions, state text, all
   probabilities, latencies, executed action, diff), `viz_NNNN.png`, `raw_NNNN.png` with `--save-raw`.

TOD request failures skip the tick (logged), never act.

## Extraction details

Sources, all on the frame reduced to native art resolution (2280/570 = 4x, nearest):
- **RapidOCR** (PP-OCRv3, onnxruntime) on the native frame upscaled 2x nearest. CPU with 4 intra-op
  threads: 0.3–1.3 s idle machine, 2–4 s under load. CUDA EP (onnxruntime-gpu) works but measured
  slower (cuDNN algo search per crop shape), so it is opt-in.
- **YOLO icon detector** `models/icon_detect_model.pt` (OmniParser-style, ultralytics, CUDA) ~170 ms.
- **Panel proposals**: Canny contours (RETR_LIST) on the native frame, boxy, 0.4–35% of frame, and
  contrasting with their surround ring (paper on desk yes, menu row on black no). Duplicates deduped,
  vertically split pieces of one paper stitched, untexted sub-parts of bigger panels dropped.
- **Merge**: same-line OCR fragments joined; icons overlapping text dropped (text wins); a paper
  (< 25% of frame) holding ≥2 text lines absorbs them and becomes one box labelled with its text;
  NMS IoU 0.5 by priority text > icon > panel.
- **Page corners**: every panel ≥1% of the frame gets a `page_corner` box at its bottom-right
  (12% of its short side, ≥48 px) — multi-page papers (bulletin, rulebook, passport) flip there.

## Known gaps (2026-10-01)

- TOD decides well on menus/intro but loops on the day-1 booth: it keeps clicking the bulletin text
  instead of the speaker/shutter/page corner; screen classification there is weak.
- The shutter lever at the right edge is not reliably detected.
- Pixel-font OCR confusions (N→H "HEXT", "Arstotzkd"); TOD copes via the image.
- The day-select screen exposes a trash (delete save) icon; nothing prevents TOD picking it.
