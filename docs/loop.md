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
     With static-layout papers (static/hybrid extractor) it is derived per frame instead (`loop.desk_target`,
     `target_source` = `derived_frame`): `layout.clear_desk_spot` scores every position of the OPEN passport
     (size from its visible box, else the 130x162 native fixture `layout.OPEN_PASSPORT`) inside `layout.DESK`
     against `layout.desk_obstacles` (open stamp bar, stamp knobs, open-tray tab, closed tray tab, inspect
     button, every other paper box; the closed tray's bar area at 1/4 weight), the data page (lower half)
     counting 3x, ties to the centre farthest from any obstacle. `layout.passport_drop_point` turns it into
     the cursor end point: from the counter the passport opens centred on the cursor (117 drops: box =
     drop - (65,81)); dragged on the desk it keeps its grab offset (box moves by drop - grab; the clipped
     visible box is extended to the full passport by `layout.full_passport_box`). The region is a small box
     (+-12 native px) on that point; `regions.desk.plan` in the tick json holds the drop point, the planned
     passport box and its `passport_obstruction` check. Before (runs 092642/115900/150111): every drop went
     to the fixed (268,267) -> open passport y 186..348, its bottom 28 px (EXP., country, number) off the
     frame and, tray open, its top-right under the bar; 9/23 next reads were readable (country p >= 0.6 or
     EXP. read). The derived spot with no other papers is (182,154)-(312,316): tray open, only the visa
     page's top-right corner (24x58) lies under the bar; the data page is fully visible.
   - `clear desk space (move the passport so its page is fully visible)` (`desk_clear`) -- the same point,
     offered INSTEAD of `desk` when an open passport lies on the desk (not under a stamp head, not already
     on the spot, IoU < 0.8) and request 1b could not read it (country unreadable / p < 0.6 and none
     carried, or Day 2/3 no EXP. date read and none carried) -- `loop.passport_needs_clear_space`. The tray
     tab's "drag left onto the desk" redirect accepts either target. While it is offered the state block
     says "Passport data page readable: no (issuing country read as ..., p=...; EXP. date not read)" and that
     the verdict cannot be decided until the page is readable; manual C/D say the same. Request 2 offline on
     092642 t2/t11 and 150111 t2 (live: passport dropped on a stamp strip, country unreadable): target now
     `clear desk space` at p 0.95-0.96.
   Each falls back to `anchors.json` (native 570x320 coords scaled to the client) if it cannot be derived;
   the tick json has `regions` and `target_source` = {name: derived|fallback}. Regions are drop targets
   only (never a source). The old generic "empty space" target is gone; the screen-centre option exists
   only as a click source on non-booth screens (cutscenes without a button).
   **Request-1 question budget (2026-10-03, audit section 4).** Booth request 1 asks only what this tick can
   consume (`loop.select_state_questions`, chosen from the previous tick's answers via `loop.probe_context`,
   never used as an answer): `passport_under_<side>` only when the pixel check (`layout.passport_under`, part
   of `screen_family`) sees a paper in that strip; `passport_shows_stamp_mark` only after a stamp press for
   this entrant (`manual.stamped` ignores it otherwise); `bulletin_or_rulebook_covering_desk` only when a
   passport and a rulebook/bulletin lay on the desk; the passport readings (`issuing_country`, photo, Day 3
   ticket) only when a paper was on the counter/desk. First tick of a run: everything. Cap `REQ1_MAX_Q`=14.
   Dropped keys are logged as `q_dropped`. Runs 092642 (D1 t0-30, D2 t95-130) + 150111 replayed offline:
   median 12 -> 9 questions per request 1.
   **Identity cache.** A paper is not re-asked while its box is unchanged: same place, same printed-text
   caption (`extract.TEXT_CAPS` on its OCR lines), IoU > 0.9 with the box seen last tick (`DOC_CACHE`, max 15
   ticks; an answer below p 0.5 max 3). The Day 2/3 1b readings (EXP./ISS./spelling/ticket) are reused when
   the same OCR candidates stand over the same unmoved desk papers as last tick (`INSP_CACHE`, max 8 ticks,
   `insp_reused_from` in the tick json). With every paper cached and no new readings, request 1b is not sent
   (replay: 35 -> 45 of 93 booth ticks without 1b).
5. `annotate` (som.py) as before (regions are never pruned). **Option hygiene (request 2):** one source per
   physical paper -- page corner, panel and icon on the same single-sheet paper (and overlapping boxes TOD
   names the same identity, `loop.paper_groups`) collapse to the largest box (replay: passport offered 2+
   times on 29 -> 0 of 93 ticks; multi-page papers keep their page corner, a click). Booth detector filler
   (unnamed icon/panel/text on no named paper with no caption/text or only a 'possibly ...' CLIP guess) is
   not offered; the rest is capped at `REQ2_MAX_OPTS`=12 sources+targets (`loop.cap_options`). The plain
   `desk` target is not offered when every offered drag source is a paper already lying on the desk, off the
   strips, and the passport's country (Day 2/3: and EXP.) is already read (`loop.desk_target_redundant`).
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
   `summary.md` with state + manual step per tick). The JSON is written in the tick; the PNGs and the overlay
   render go to a log thread (`_LOG_POOL`, ~0.5-0.75 s off the tick), flushed before `summary.md`. `manual_step_for_state` in the json is
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

Generic cycle guard (`loop.CycleDetector`, always on; run 115900 t118-142 toggled the tray 25 ticks, every
drag "changed" pixels so no per-case rule fired). Per executed tick it keeps a signature (screen, request-1
yes/no facts `CYCLE_STATE_KEYS`, manual step letter, executed input + source description + target, with the
TOD score and position tail stripped) and a progress signature from the loop's own entrant bookkeeping
(`cycle_progress`: entrant resets, passport under a stamp, stamp presses, hand-back, G2 -- never gt). Two full
periods of a 2- or 3-signature sequence (A,B,A,B / A,B,C,A,B,C) inside the last 8 consecutive ticks with no
progress change = a cycle: the callout "the last N actions alternated X and Y with no progress; neither is the
answer" goes into the history block (`CYCLE:` line, 6 ticks), the cycle's sources are excluded for 6 ticks
(RULED OUT, "was part of a repeating cycle"), and the tick json gets `cycle_break`. A second cycle for the same
entrant stops the run (`stop reason: cycle: ...`). It offers nothing new.

Step N (no passport presented, `manual.no_passport`): decided from TOD's request-1 answers, not inferred by
the loop. Request 1 asks the noul `no_documents_presented` while a person is (or was last tick) at the window;
step N holds when it is >= NO_DOCS_P (0.6), TOD does not say a document lies on the counter shelf, and no paper
TOD named the passport (run 092642 t9). Its sub-steps come from two more TOD questions, asked only on the tick
after no_documents_presented was yes (`NO_DOCS_KEYS`): `rulebook_page` (choice: not_open / basic_rules / other
page) and `interrogate_prompt_visible`, plus `inspect_mode_on`: N1 rulebook not open -> drag it onto the DESK;
N2 another page -> click the page corner; N3 BASIC RULES -> click the inspect button; N4 inspect mode on -> click
the passport rule line, then the empty counter; N5 prompt visible -> click it (docs/game.md, missing-document
interrogation). The state block says "The person has presented no documents" (and, at N1, names the desk as
the rulebook's destination). While it holds: tray tab, stamps and the stow shelf ('counter shelf left of the
desk', it puts the rulebook away) are not offered; the rulebook slot, transcript printer, inspect button, the
desk and layout.py's click-only `counter_empty` element (pixel test: nothing on the shelf; Day 2+) are;
rulebook elements are not drag-only, and the rulebook gets no "put it away" hint.
Offline `--sequential` carries entrant memory, history, exclusions and the cycle guard across consecutive
frames of one run (each decision counts as executed and changed).

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

## Day 2/3 passport readings in request 1b (2026-10-03, loop8)

- `exp_year` / `exp_month` / `issuing_city` left request 1. On Day 2/3 booth ticks, request 1b (`doc_probe`, after
  extraction, parallel to request 1) asks `manual.inspection_doc_questions(desk_text)`:
  `exp_date` = choice over the full dates the desk OCR read ("EXP. 1983.12.13" ...; + none),
  `issuing_city_tok` = choice over the OCR's city-like words in the OCR's own spelling (+ none), and, only when the
  first token differs from the nearest rulebook name, `issuing_city_spelling` (S1 = OCR spelling, S2 = rulebook
  spelling, S3 = other; neutral labels). Options are strings read from the screen; the code compares the picked
  date with today and looks the picked city up in the rulebook table (`read_inspection_answers`, `expiry_valid`,
  `check_value`). A non-rulebook spelling denies only at p >= DENY_P. No gate on `passport_open_readable`: the
  questions exist only when the OCR found a date/city, and `none` covers the rest.
- With these questions in 1b, the "READABLE TEXT ON THE DESK" block is left out of the 1b text (listing the OCR's
  misreading 'Paradizng' pulled the spelling answer to S1 on two valid passports).
- Dry run on the 30 gt-labelled Day 2 frames (16 entrants): expiry 28 right / 0 wrong / 2 undecided (was 3/30),
  city 29 / 0 / 1 (the undecided frame had no country reading; live carries it). Photo unchanged.

## `--pause-think` (harness timing feature, off by default; loop8, 2026-10-03)

Not a TOD decision and not game input chosen by anyone: a harness pause so the game clock does not run while the
loop is waiting for extraction and TOD. Per booth tick:
1. grab the frame (game running) -> this is the only frame extraction and TOD ever see;
2. press Esc -> the game's own pause menu; the loop waits until the screen differs from the grabbed frame by
   >= 5% (`PAUSE_MIN_FRAC`, measured 0.73) and otherwise presses Esc again and runs that tick unpaused;
3. extraction + request 1 / 1b / 2 as usual;
4. press Esc -> resume; the loop waits until the frame matches the grabbed one again (< 5% changed, measured 0.000;
   one retry), then executes TOD's input and the post-wait verification.
Every pause/resume is printed (`[pause] ...`) and logged in the tick json (`pause`: opened, menu_frac, held_s,
resumed, resume_frac). A tick that ends early (skip/stop) and the loop exit resume the game first.
Checked with ground truth (eval only): the clock held 06:36 for 3 s under the menu and ran on after the resume.
Reason: a day is 4-6 real minutes (~3 game-min per real second) and entrants after 18:00 are unpaid; at ~6.7 s per
tick the runs ended Day 2 with savings 0 / 10 / 15 / -5 (game over).

## Desk clutter (step K) and the stamp-mark recheck (loop10b, 2026-10-03)
Citation slips and the Pink Vice flyer are a TOD-handled situation, not a stall (164732 t92-121: 32 ticks, cycle
stop; 161058 Uvilia: 71 ticks).
- `clutter_facts` (geometry only, on TOD's request-1 identity citation/flyer p >= 0.5): lies on a landing strip,
  under the open tray bar (>= 30% of its box between the bar top and the strip), on the passport, or elsewhere;
  `in_way` = strip / bar area / passport. No new question: the identity questions already name the papers.
- State block: each such paper gets a tail saying whose it is, where it lies, and the move (stow shelf; tray first
  when it is under the open bar). Manual step K: close tray (if under the bar) -> drag slip/flyer to "counter shelf
  left of the desk" -> continue. `situation` returns K1/K before the tray steps while the passport is under no stamp.
- Options: an in-the-way citation stays a (drag-only) source; out-of-the-way citations are still hidden (104848).
  The stow target caption now names citation slips; in G2 it stays offered while a citation is in the way.
  `decide`: a citation/flyer is never dropped on a landing strip / tray stow edge, a citation never on the entrant,
  a flyer only after the passport went back (G2) -> TOD's best other target, else wait. `passport_sides`: a strip
  paper TOD names citation/flyer (p >= 0.5) is not the passport unless passport_under >= 0.85.
- Stamp recheck: after `man.UNDECIDED_RECHECK` (3) E? ticks in a row (`Entrant.note_step`), request 1 adds
  `passport_stamp_ink` (approved / denied / none; the yes/no mark question sat at 0.20-0.54 on the inked Uvilia
  passport in the 163640 dry run while this choice read DENIED 0.53-0.84). Ink side p >= 0.75 -> `facts['mark_side']`: stamped from the screen alone (F, or F2 for APPROVED ink with a DENIED verdict),
  stamps hidden otherwise, the entrant offered for the hand-back. One extra question, only on those ticks.
