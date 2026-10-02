# Agent loop: extract → Set-of-Mark → TOD → click

Modules (`src/tod_papers/`):

| file | role |
|---|---|
| `extract.py` | `extract(frame_bgr) -> list[Box]`. Box = `(x1,y1,x2,y2,text,kind,conf,parent)`, client-relative physical px. |
| `som.py` | `annotate(frame, boxes, max_marks=60) -> (annotated, {id: Box})`. Numbered tags + outlines, ids in reading order. |
| `loop.py` | The agent. Two TOD requests per tick (one image each): state on the plain frame, action on the SoM frame. Executes the pick via `io_win.click/drag` at the box centre. |
| `manual.py` | The Papers, Please playing guide sent in full with every action request, the state questions, the state block, the click-only / drag-only convention. |
| `anchors.json` | Fallback drop regions (native 570x320 coords) used only when a region cannot be derived from detected boxes. |
| `overlay.py` | Writes `viz_NNNN.png`: source-probability heatmap (red fill) + target probability (blue outline) + legend. |

## Running

```
cd .
.venv-loop\Scripts\python.exe -m tod_papers.loop --max-ticks 10
.venv-loop\Scripts\python.exe -m tod_papers.loop --dry-run --max-ticks 3   # no input at all
```

Flags: `--max-ticks N`, `--dry-run`, `--max-marks 25` (soft element cap), `--history 30`, `--send-width 1140`
(annotated frame is downscaled to this before upload), `--send-format png|jpeg` (png: ~50-80 KB on pixel art
vs ~170 KB jpeg, cv2-encoded in ~20 ms), `--settle 0.12` (s between
hover / down / up so Unity sees them on separate frames), `--post-wait 0.8`, `--save-raw`, `--no-viz`.

Offline (no game window, no input): `python -m tod_papers.loop --frames runs/<ts>/raw_0000.png ... [--out DIR]`
runs probe + extract + one agent request per saved frame and writes `<frame>.json` / `<frame>_som.png`.

Env: `TOD_OCR_THREADS` (default 4), `TOD_OCR_GPU=1` to opt into the onnxruntime CUDA EP (slower here,
see below). TOD key comes from `TOD_API_KEY` / `.env` via `tod_client.py`.

The game must be running windowed (UnityWndClass, title `PapersPlease`); launch e.g.
`PapersPlease.exe -screen-fullscreen 0 -screen-width 2280 -screen-height 1280 -popupwindow`.

## venv

`.venv-loop` is a thin venv layered on `.venv-extract` through a `.pth` file (torch/ultralytics/
rapidocr/opencv come from there) plus `pywin32 dxcam mss comtypes`. Recipe in `requirements-loop.txt`.

## Per tick (since 2026-10-02, loop5)

1. `focus(hwnd)` and verify `GetForegroundWindow()==hwnd` (Desktop Duplication captures the screen
   region, so an occluded game = wrong frame). Abort (exit 2) after 3 misses.
2. Park the cursor, wait until the frame is still (<=3 s), grab.
3. **Request 1 -- state** (unmarked frame downscaled to `--send-width`, one-line text), concurrently with
   `extract`. TOD answers from the picture; nothing is inferred from detector labels:
   `screen`, `day`, `person_at_window`, `document_on_counter_shelf`, `document_open_on_desk`,
   `stamp_tray_open`, `document_under_stamp_heads`, `passport_shows_stamp_mark`,
   `bulletin_or_rulebook_covering_desk` (noul), and the inspection decisions `issuing_country`
   {ARSTOTZKA, other}, `expiry_after_today` (today = date of the known day, else day 1), `photo_matches_person`.
   All answers + probabilities go to `tick_NNNN.json` (`state`, `state_line`) and the overlay banner.
   If request 1 fails the tick is skipped (never act on a stale picture).
4. **Drop-target regions** (`derive_regions`), booth screens only:
   - `stamp landing strip (under the APPROVED/DENIED stamp head)` -- one per stamp, directly beneath the
     detected stamp body (x span of the OCR `APPROVED`/`DENIED` / stamp-caption boxes, y from the body's
     bottom edge down 12% of the frame). Offered when request 1 says the tray is open or a stamp is detected.
   - `counter shelf (hand documents back here)` -- top = bottom of the person box at the window (or a
     document lying on the counter), right = the shutter lever / window frame, bottom = drawer row.
   - `desk (drop documents here to read them)` -- free desk right of the shelf, below the tray.
   Each falls back to `anchors.json` (native 570x320 coords scaled to the client) if it cannot be derived;
   the tick json has `regions` and `target_source` = {name: derived|fallback}. Regions are drop targets
   only (never a source). The old generic "empty space" target is gone; the screen-centre option exists
   only as a click source on non-booth screens (cutscenes without a button).
5. `annotate` (som.py) as before (regions are never pruned).
6. **Request 2 -- action** (SoM frame). Text = `manual.build()`: the whole manual (~1.2k words: screen
   layout, click-only vs drag-only elements, drop targets, the entrant cycle A-G, APPROVED/DENIED rules for
   days 1-3, bulletin/rulebook/page corners, other screens, mistakes seen in run 20261002_003519) + a
   "WHAT IS CURRENTLY TRUE ON SCREEN" block from request 1 + the last 30 actions, one line each
   (`tick | state summary | input | element | effect`) + ruled-out elements. No rule is pre-selected for
   TOD. Questions: `action` {click, drag, wait} (instruction states the click/drag constraint),
   `source` (element ids), `target` (element ids + regions).
7. **Click/drag convention enforced in code** (`enforce_input`): horn, stamps (OCR APPROVED/DENIED,
   stamp captions), buttons, page corners are click-only; documents (passport/paper/rulebook captions,
   texted desk panels), the tray tab and the lever are drag-only. On a conflict the loop takes the better
   of (a) same element with its allowed input, p(src)*p(action'), and (b) same input with the best
   compatible element, p(action)*p(src'). TOD's raw pick and the note are logged (`tod_pick`,
   `input_convention`) and shown in the overlay.
8. Execute, verify by frame diff, log (`tick_NNNN.{png,json}`, `viz_NNNN.png`, `raw_NNNN.png`,
   `summary.md` with state + manual step per tick). `manual_step_for_state` in the json is
   `manual.situation(state)` -- a diagnostic of which manual line applies; it is never sent to TOD.

Offline: `python -m tod_papers.loop --frames a.png b.png --out DIR` runs request 1 + extract + request 2 per
frame and writes `<frame>.json`, `<frame>_som.png`, `<frame>_viz.png`, `offline.json`.

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

- (10-02) The entrant's passport lying on the counter (`221705/raw_0030`) gets **no box**: YOLO and the
  panel proposals miss it and Grounding DINO scores it as "rubber stamp" 0.25 (< 0.30). CLIP labels the
  rulebook "passport booklet", so with the screen right TOD picks the rulebook. Needs an extract.py fix.

- TOD decides well on menus/intro but loops on the day-1 booth: it keeps clicking the bulletin text
  instead of the speaker/shutter/page corner; screen classification there is weak.
- The shutter lever at the right edge is not reliably detected.
- Pixel-font OCR confusions (N→H "HEXT", "Arstotzkd"); TOD copes via the image.
- The day-select screen exposes a trash (delete save) icon; nothing prevents TOD picking it.

## Harness reset: `tools/reset_game.py` (2026-10-02)

Puts the game into a known clean state with **no scripted in-game clicks**:

```
.venv-loop\Scripts\python.exe tools\reset_game.py              # restart at the main menu
.venv-loop\Scripts\python.exe tools\reset_game.py --to-booth   # ... then TOD plays menu -> Day 1 booth and stops
```

1. Terminates `PapersPlease.exe` (psutil terminate, kill after 10 s).
2. Copies `%APPDATA%\3909\PapersPlease\` to `runs/save_backup_<ts>/`, then deletes the progress files
   (`save_*.sav`, `headers.sav`, `names.sav`, `stats.sav`). `settings.sav` (video/window settings),
   `steam_autocloud.vdf` and screenshots stay. `--keep-saves` backs up without deleting. The files are
   encrypted hex blobs, so nothing is edited in place. Steam Cloud (autocloud) could restore deleted files at
   launch; the tool lists the folder after the relaunch so that is visible in its log.
3. Relaunches with `steam://rungameid/239030` (a direct exe launch is restarted by the DRM).
4. Waits for the `UnityWndClass` window of the new process, then for a non-black, still frame (4 consecutive
   0.5 s grabs with < 0.4% changed pixels; an animated menu only gives a warning), brings it to the
   foreground and checks the geometry: client size vs 2280x1280, 570:320 aspect (otherwise the game
   letterboxes and the `anchors.json` fallbacks, scaled by W/570 and H/320, would be offset), integer
   native scale, and fullscreen (client == screen).
5. `--to-booth` runs `loop.main(["--max-ticks", 40, "--stop-on-screen", "booth_idle,documents_on_desk",
   "--stop-consecutive", "2", "--save-raw"])`. Every click on the way (STORY, day tile, NEXT, WALK TO WORK)
   is TOD's pick. The loop stops without acting on the second consecutive booth tick. Exit 3 if the booth
   never came.

New loop flags: `--stop-on-screen a,b` + `--stop-consecutive N` (stop without acting once request 1 says one
of those screens N ticks running) and `--stall-stop N` (stop when the screen + state summary stay identical
for N ticks). The stop reason goes into the tick json (`stop_reason`) and `summary.md`.

New derived elements (geometry from detected boxes, logged like the other regions):
- drop target `right edge of the desk (drag the tray tab here to put the stamp tray away)` (`tray_stow`):
  right of the detected APPROVED stamp, at the stamp height; fallback `anchors.json` `tray_stow`. Offered
  with the landing strips while the tray is out.
- source `stamp tray tab (left end of the open stamp bar)`: while the tray is out its tab moves to the left
  end of the bar, where no detector draws a box. Derived just left of the detected DENIED stamp, only when
  request 1 says the tray is open (no fallback). Drag-only.

Manual (2026-10-02): Day 1's only rule is issuing country ARSTOTZKA -> APPROVED, else DENIED; expiry and
photo start on Day 2. Request 1 still asks and logs `expiry_after_today` / `photo_matches_person`, but on
Day 1 (or unknown day) the state block leaves them out. Both landing strips are valid; the passport goes
under the stamp TOD intends to use. Recovery rule D2: passport no longer visible -> close the tray (drag its
tab right) to reveal it.

## Desk OCR, stamp detection, hand-back (2026-10-02, loop6)

- **Order per tick changed**: `extract` runs first, then request 1 (it needs the OCR). Costs ~1-3 s per tick.
- `desk_facts(boxes)`: OCR text of every document on the desk (x > 0.34 W, y > 0.45 H; stamp bodies on the tray,
  the ALIGN VISA BENEATH STAMP label and the drawer readouts excluded). It goes into BOTH request texts as a
  "READABLE TEXT ON THE DESK" block, with a note when a country-like token is present
  (`ARSTOT2HA` -> ARSTOTZKA; digit->letter normalised; ARSTOTZKA/KOLECHIA/IMPOR/ANTEGRIA/OBRISTAN/REPUBLIA/
  UNITED FEDERATION).
- **Inspection gating** (`inspect_keys`, `gate_inspection`): `issuing_country` is asked only when the desk OCR
  contains a country token; `expiry_after_today` / `photo_matches_person` when it contains a country token or
  passport fields (ISS/EXP/DOB/ID#). Answers are used only if request 1 says `document_open_on_desk >= 0.6`
  (otherwise logged as `inspect_dropped`). Stricter than "country OR fields" for the country question on
  purpose: run 083908 t8 had EXP visible but the country off-frame and read "other".
- **Entrant memory** (`Entrant`): the most recent issuing-country reading with p >= 0.85 is carried forward
  ("Passport read as ARSTOTZKA at tick 9" in the state block and a "THIS ENTRANT SO FAR" line at the top of the
  history). A click on a stamp (OCR/caption side) whose effect was `changed` while request 1 said tray open and
  passport under the heads is recorded as a stamp. A changed drag of a document onto the counter shelf after a
  stamp marks the hand-back. Reset when the window is empty.
- **Stamped = any of three** (manual rule F): TOD's `passport_shows_stamp_mark`, `stamped=yes (OCR)` (APPROVED/
  DENIED-like word below the stamp bar, e.g. `DENETAS` = DENIED ink over ENTRY VISA), or the history line
  "A stamp was clicked at tick N ... the passport is stamped". F2: APPROVED by mistake -> click DENIED (DENIED
  overrules APPROVED, Fandom "Entry denial"); DENIED by mistake cannot be undone -> hand back (first two
  citations a day are warnings).
- Descriptions: a desk sheet whose OCR has passport fields or a country is described as
  "the entrant's PASSPORT (open; drag it)"; stamp-ink text as "DENIED stamp ink on the passport page".
- On booth screens the `target` options are the drop-target regions only.
- **Hand-back target = the person, not the shelf.** Run 092612 dropped the stamped passport on the counter
  shelf 5 times; it lay there closed and the entrant never took it. Dropped on the person (face/torso box) the
  entrant said "Thank you." and left. The region `hand_back` ("the entrant at the booth window -- drop
  documents ON THE PERSON to hand them back") is derived from the `person face` box (fallback
  `anchors.json` `hand_back`) and offered on every booth frame with a person; the counter shelf is no longer
  a target (still used to place the desk region). The entrant memory records the hand-back on a changed drag
  onto `hand_back`, and resets on the horn only when the window is empty.
- **passport_under** (`passport_under(page_xr, regions)`): which stamp heads the passport page lies under
  (x-overlap of the OCR'd page text with each landing strip >= 40 %). Shown in the state block ("lies under
  the APPROVED head; NOT under DENIED ..."); a stamp click is recorded only for a head the passport was under.
- **Tray hints**: the closed tray's "tab at screen edge" is described as the stamp tray tab; the state block
  says "desk text shows passport fields: an open passport IS lying on the desk" when request 1 says not
  open, and "passport already open and tray closed: next step is C".
- The APPROVED ink on the passport reads **"ENTRY GRANTED"**; the OCR ink regex matches GRANT/RANTED too.
- Remote extraction hook: with `TOD_EXTRACT_URL` set, `extract`/`warmup` come from `extract_remote`
  (cloud L4, see remote_extraction.md).
- Foreground guard: waits (2 s polls, no input, no tick spent) up to `--fg-patience` (default 60) before the
  safety abort.
