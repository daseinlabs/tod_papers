# Agent loop: extract → Set-of-Mark → TOD → click

Modules (`src/tod_papers/`):

| file | role |
|---|---|
| `extract.py` | `extract(frame_bgr) -> list[Box]`. Box = `(x1,y1,x2,y2,text,kind,conf,parent)`, client-relative physical px. |
| `som.py` | `annotate(frame, boxes, max_marks=60) -> (annotated, {id: Box})`. Numbered tags + outlines, ids in reading order. |
| `loop.py` | The agent. Up to three TOD requests per tick, one image each: request 1 (state, plain frame), request 1b (paper identities, Day 2/3 readings, the verdict; plain frame), request 2 (action, SoM frame). Executes TOD's input via `io_win.click/drag`. |
| `manual.py` | The Papers, Please rules (static, ~2.4k chars) + the per-tick "what applies now" block sent with every action request, the state / verdict / identity questions, the state block, the click-only / drag-only convention (stated as text; only logged). |
| `overlay.py` | Writes `viz_NNNN.png`: source-probability heatmap (red fill) + target probability (blue outline) + legend. |

## Decision path: what TOD decides and what code does (loop15, 2026-10-03)

Fixes for every A item and the listed borderlines of `tod_decides_audit notes`. One tick:

1. **Request 1** (plain frame): screen, day, person / counter / open-on-desk / tray / inspect, `passport_under_<side>`
   (always asked while the tray is open on the pixels), issuing country, Day 3 ticket choice, step-N questions,
   `passport_stamp_ink` (STAMP_INK_Q: approved / denied / none) after a press, once ink was read, or while the
   passport lay under a stamp head, and `passport_returned` (returned / still_here / no_person) after a drop on
   the person, a press, ink, or a hand-back. First tick of a run: all of them.
2. **Request 1b** (plain frame, parallel to request 1 live): paper identities (`doc_question`: the paper's OCR lines
   verbatim, each choice defined by its printed text), Day 2/3 readings over the OCR strings (EXP. date, ISS.
   city, spelling, ticket VALID ON), and the **verdict** question (`manual.verdict_question`): choice approved /
   denied / cannot_decide_yet; its text is today's rule (Day 1: Arstotzkans only; Day 2: passport valid = not
   expired and ISS. city in the rulebook list for its country; Day 3: + entry ticket VALID ON 1982.11.25 for
   foreigners) and TOD's own earlier readings of this entrant (country, EXP., city, ticket with tick and p,
   labelled "your answer at tick N"; "not read yet" otherwise). Asked once the passport was seen (country read
   earlier, open on the desk last tick, or a desk paper named passport) until the hand-back
   (`loop.set_verdict_ask`). No code compares a date, looks up a city or computes a verdict any more
   (`needed_stamp`, `wrong_stamp`, `undecided_stamp`, `expiry_valid`, `check_value` were deleted).
3. **Identity gate**: an identity answer below `manual.IDENTITY_MIN_P` (0.4) makes the paper `unread`
   (`loop.name_docs`). It is offered as "document on the counter -- unread (TOD could not tell, p=...)", never
   counted as ticket / flyer / passport by any step, the state block says it is not known, and manual step B
   says to drag unread papers to the desk so they can be read. The old B3 cap (`b3_n`) is gone.
4. **State block** (request-2 text): facts only, each TOD reading labelled "your reading"; TOD's own verdict answer
   and p ("Your verdict for this entrant (your own answer, this frame|tick N)"); "stamped" = TOD's executed
   stamp press (loop16, user decision: "Stamp pressed: APPROVED at tick N"); the stamp-ink reading is shown as an
   informative fact only ("Stamp ink on the passport (informative): ...", with "the press may have missed" when a
   press is on record and no ink side was read) so TOD can see a missed press and press again. Removed:
   "Section 5 applied ... -> X", "the passport is under the wrong stamp: drag it onto X", "the next step is C",
   "Click the stamp the passport is lying under", "The decision is not known yet ... the stamps are not offered",
   the LOOP WARNING, "EXPIRED / not expired" and "(NOT) a valid issuing city".
5. **Options** (loop20, `stamp_hidden`): the stamp offered is the one TOD's OWN verdict of this tick names (request 1b,
   p >= 0.5); the other stamp is struck through in the SoM, and both are while the verdict is `cannot_decide_yet`,
   below 0.5 or not asked; the verdict stamp too while the passport is not under it
   (`hidden_by: passport_not_under=approved (...)`, the press_gate strip refusal moved before request 2; 233434 t20-21). Logged as `stamp_hidden` = `hidden_by: tod_verdict=approved p=0.81` (tick json) and a
   RULED OUT line in the state block ("the stamp you can press is the one your own verdict names; change your verdict
   if you disagree"). Both landing strips stay drop targets. Code never chooses the verdict (runs 232544 / 233434:
   approved 0.65-0.84, DENIED pressed 5x, refuse-stop). No ink-based hiding. Stall rule (loop21, `REPRESS_MAX` 2):
   after 2 executed presses of the verdict's stamp for this entrant that stamp is struck too
   (`hidden_by: pressed_2x=denied (ticks ..)`, RULED OUT: "the passport counts as stamped, a further press adds
   nothing"; run 012545 t40-58: DENIED pressed 11x, ink under the bar read 'none', no hand-back).
   Request-2 text budget (loop21, `manual.build`): last 15 actions, lines <= 140 chars, desk OCR capped, the part
   after the manual <= `STATE_BUDGET` 7000 chars (oldest history dropped first); `text_chars` in the tick json.
   Loop22 manual rewrite: the static booth manual (`manual.BOOTH_MANUAL` + `DAY_RULE_TEXT[day]`, ~2.4k chars, was
   17.1k) is RULES only -- what is where, the input line, today's admission rule, the per-entrant goal, 3 time
   wasters; no lettered steps. What applies now is `manual.now_block()`: 1-4 plain sentences after the state block,
   chosen by `situation()` and worded from TOD's own answers (verdict, strip, presses, clutter, hand-back); only the
   matching situation is stated, never a step letter. `situation()` letters stay for logs / cycle signature.
   Non-booth screens get `manual.OTHER_SCREENS` (~1k chars).
   A tray toggle loop (>= 3 open/close in 8 ticks without a press) strikes the CLOSING tab through via
   `stuck.ban` before request 2 (4 ticks) with the reason in RULED OUT and a `TRAY LOOP:` line at the top of the
   history. The hand-back target is offered once a stamp press of TOD's executed for this entrant (or in G2 / step N);
   it is never gated on an ink re-read (runs 210453 t20 / 211624 t17: the APPROVED press inked the visa page,
   STAMP_INK_Q read 'none' 0.59-0.89, nothing was handed back until 18:00). Desk regions on the
   vision extractor come from the frame or are dropped (`target_source: dropped`); `anchors.json` was removed.
6. **Request 2**: `action` (click / drag / wait; its question text is the manual section 2 line
   `manual.INPUT_LINE` "Click for stamps/buttons/horn/page corners, drag for papers and the tray tab."), `source`
   (each element's option text ends with its input as a screen fact from its class, `manual.affordance_text`:
   "— click (press to stamp)", "— click", "— drag"), `target`. `decide` executes TOD's answers: no tab re-pick, no tab-target rewrite,
   no desk re-drop re-pick. A convention mismatch is logged (`convention_mismatch`, note); a click on a drag
   element (paper, tray tab, lever) is a drag to TOD's target; a drag on a press-only element is a click (input conventions
   below). A citation/flyer dropped onto a strip / the tray edge / (citation) the entrant is
   REFUSED (no input, logged), never redirected.
7. **Guards that remain** (they refuse or exclude, never pick): stamp press (`press_gate`, loop18) refused unless
   TOD's verdict answer of THIS tick (p >= 0.5) names that stamp -- `cannot_decide_yet`, the other stamp or no
   verdict this tick -> refused, history callout "refused: your verdict this tick was X (p)" -- and unless TOD's
   strip answer puts the passport under that stamp (backstop; the verdict hiding of step 5 comes first). Code never chooses the verdict; the
   verdict is asked every tick the passport is readable, so TOD can change its mind. Request 2's action question
   starts with TOD's own line "Your verdict this tick: X (p)"; the landing strips stay "stamp landing strip (under
   the APPROVED/DENIED stamp head)" with nothing about the verdict. On an entrant-memory reset tick that tick's
   verdict and readings are discarded (not stored, verdict removed from the state: loop17 L3, Narovska's approved
   0.60 carried to Jarvinen); delete/trash veto on menus; stuck / repeat-drag / cycle exclusions; stop rules.
8. **Entrant memory**: carries TOD's own readings and answers (country, EXP., city, ticket, ink, verdict) and
   every executed stamp press (`stamp_clicks` [(tick, side)], recorded when the press input was sent on a stamp
   the passport lay under, whether or not the pixel check saw a change). The history block (last 30 actions,
   `--history 30`) shows the press ("APPROVED pressed at tick N (passport counts as stamped APPROVED)") and its
   effect, and the state summary of later lines carries "STAMPED(APPROVED pressed tN), ink read none p=..". The
   hand-back is TOD's `passport_returned` answer: `returned` -> entrant done (`handed_back`); with an entrant paper
   (passport / ticket / flyer / unread) still named on the desk or counter while the person stays -> G2
   (`waiting_docs`); `still_here` -> the drop was not a hand-back. HANDBACK_STAY / HANDBACK_DOCS_STAY and the
   Day-3 "no passport, ticket left" combination rule were removed.
   loop25: the tick right after a drop of the paper TOD named the passport onto the person (pp_drop), with a
   stamp on record, TOD naming no passport anywhere outweighs a `still_here` below PP_GONE_STILL_P = 0.8 -> returned
   (run 043045 t110-118: Lena Kariska handed back, `still_here` 0.72, step F asked for a gone passport 8 ticks).
   Put-away sliver (loop25, run 044911 t62-66): a flyer/citation box under 40% of its full size that touches (6 px)
   a paper of the same kind lying on its put-away spot this frame is part of it (put away, `sliver`), not step K.
9. `passport_sides` (strip, loop20): the passport is under a head when TOD's `passport_under_<side>` p >= 0.5 OR
   TOD's identity of the paper over that strip is "passport" (p >= 0.5) AND the pixel test finds paper on the strip
   (run 233434 t22: strip "not passport" 0.73, identity passport 0.90 -> correct APPROVED press refused). A paper
   TOD's identity names as not-passport (p >= 0.6, strip p < 0.75) or clutter is excluded; the state block then
   says to drag it to the desk first (E0; the desk target stays offered while a paper lies on a strip).
   `strip[side].source` = `tod_strip` / `tod_identity+pixel` / `none` / `tod_identity_*` in the tick json.

### Input conventions (loop16)

TOD's pick is the ELEMENT; actuating it is the input layer's job. `decide`:
- `action=drag` on a press-only element -- a stamp, the horn, a button, a page corner, the inspect toggle, a day
  tile, NEXT/CONTINUE text (`_cls` == click) -- is executed as a click, logged
  `input_convention="drag->click (press-only element)"` and `convention_mismatch`; TOD's own answer stays in
  `tod_pick` (run 211624: 20 APPROVED stamp drags vs 6 clicks). The stamp-press guard (passport under that stamp)
  applies to the click.
- mirror (loop22): `action=click` on a drag-only element -- papers, the tray tab, the lever -- is executed as a drag
  to TOD's own `target` pick, logged `input_convention="click->drag (drag-only element)"`; TOD still chooses element
  and target (dry run 004042 t5-30 on the rewritten manual: 4 conversions, 0 paper clicks left as clicks).
- Empty window: the rule line and the now-block say "click the loudspeaker ...; waiting achieves nothing"; no
  "(clicking it does nothing)" phrase remains (dry run: horn on 8/8 empty-window ticks).

### Strip drop geometry (loop16)

`strip_plan` (loop.py) replaces the old visa-centre-on-the-region-centre point: the sheet is the FULL open passport
(`layout.full_sheet_box`) anchored on its visible box -- a box trimmed by a paper lying across it (5d1669c splits
the passport around the entry ticket) or cut by the stamp bar extends past the covered edge, uncovered edges are
real; the grab point lies on the passport, not on the paper across it (`layout.passport_grab_point`); the visa page
(upper half) is centred on the head footprint (`layout.head_footprint`: STRIP_X width centred on the detected
stamp box, rows STRIP_Y), the data page kept on the frame; `strip_plan` in the tick json logs grab / drop /
planned / visa / foot / `inside` (visa page contains the footprint) / `trimmed`. The desk drop (`desk_target`)
uses the same sheet anchoring and grab point. A paper lying across the open passport (`paper_on_passport`, >= 30%
of that paper on the sheet) is a state-block fact and gets its own target "clear desk space off the passport"
(`desk_aside`, a clear_desk_spot for that paper with the passport as obstacle); manual E: move it off first.
Measured (loop16 frames): after the strip drag the passport sat at x 420-561 (Day 3, 210453 t19 / 211624 t17)
vs 429-568 (Day 2, 164732 t43), top under the bar edge (y 212) in both: -8/-9 px in x, 0 in y; the visa page
covered the APPROVED footprint (455-521) in both, and the Day 3 presses did ink (green mark visible at the visa
top, 210453 t21, 211624 t18). The 205418 "t14 press" was a stamp drag with an empty strip.

Still code (borderline, by design): question selection (which questions are asked, from the previous tick and a
pixel tray test), drop-point geometry (desk spot, strip point), fixed-layout controls and targets in the hybrid
extractor (`target_source: static`), the OCR citation regex that hides out-of-the-way citation boxes (B12), the
horn / storage hiding while a person is at the window (B4), the inspect-button hiding (B5), step-N hiding, the
G2 tray-open hiding (B9, now keyed on TOD's returned answer), carry thresholds (country 0.6, non-rulebook
spelling DENY_P 0.75), and `situation()` (diagnostic step letter, never sent).

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
     says "Passport data page readable: no (issuing country read as ..., p=...; EXP. date not read)" (loop15:
     the verdict sentence is gone; TOD's own verdict answer says cannot_decide_yet); manual C/D. Request 2 offline on
     092642 t2/t11 and 150111 t2 (live: passport dropped on a stamp strip, country unreadable): target now
     `clear desk space` at p 0.95-0.96.
   A region that cannot be derived is not offered (loop15; `anchors.json` removed); the tick json has `regions`
   and `target_source` = {name: static|derived|derived_frame|dropped}. Regions are drop targets
   only (never a source). The old generic "empty space" target is gone; the screen-centre option exists
   only as a click source on non-booth screens (cutscenes without a button).
   **Request-1 question budget (2026-10-03, audit section 4).** Booth request 1 asks only what this tick can
   consume (`loop.select_state_questions`, chosen from the previous tick's answers via `loop.probe_context`,
   never used as an answer): `passport_under_<side>` whenever the tray is open on the pixels (loop15: no longer
   dropped by the pixel strip test); `passport_stamp_ink` / `passport_returned` per the decision path above; `bulletin_or_rulebook_covering_desk` only when a
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
   TOD. Questions: `action` {click, drag, wait} (instruction states the click/drag convention),
   `source` (element ids), `target` (element ids + regions).
7. **Click/drag convention: logged, not enforced** (loop15; `enforce_input` was dead code and is deleted):
   TOD's `action` answer is executed. When it disagrees with the element class (`manual.input_class`, TOD's
   paper identity) the tick json gets `convention_mismatch` and `input_convention`; the input is not changed.
   Loop16 (B26): the class is also stated in each option's text and the manual's input paragraph is one line;
   dry run 9 frames (201459 t10/26/28/40/50/60, 200239 t5/10/42): mismatches 4 -> 1.
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
Loop26 persistence: once step N held for an entrant (`Entrant.nodocs_on`; also started by the rulebook open on
BASIC RULES with no paper of theirs, p >= 0.5) it holds while no_documents stays >= NO_DOCS_KEEP_P (0.4) and the
person is there, with no fresh 3-tick streak; the sub-state questions stay asked. N4 is split: N4 click the rule
line (offered click-only), N4b (an executed click on a 'must have a passport' line in inspect mode) click
`counter_empty`. From N3 on the rulebook slot and the desk page corners are not offered, the rulebook's lines only
in N4, `counter_empty` only in N4b. The furthest sub-step reached counts as progress for the stall / cycle rules.
Loop28 (run 070003 t143-158, inspect mode darkens the booth: person 0.48 -> H left inspect mode 15 ticks): while
`nodocs_on` and inspect_mode_on >= 0.5 the person counts from 0.3 (also for the memory reset) and unread boxes
(identity < 0.4) are no entrant paper; `nodocs_inspect_boxes` offers exactly one click -- N4 the rule line (the
OCR rows 'Entrant must have a' + 'passport' merged into one option), N4b `counter_empty` (layout element if the
pixel test fails), N5 a box reading 'interrog...' -- never the inspect button; the dynamic block is one sentence.
Loop28 N3a / N4a (run 074339 t138-193: BASIC RULES open with its left page under the booth edge, only 'RULES'
read): `facts['nodocs_rule_visible']` = a frame OCR box reads 'must have a pass' / 'Entrant must have'. Not visible
-> N3a drag the rulebook onto the desk target (inspect button not offered), or in inspect mode N4a click the
inspect button only (leave); visible -> N3 / N4 as before.
Loop29: step N also starts when 3 of the last 4 ticks' 'no documents' answers are >= 0.5 with one >= 0.6
(`Entrant.nodocs_hist`; run 080225 t149-159 hovered 0.52-0.70), or after 4 ticks with a person, no passport /
ticket / unread paper named and the counter empty (`nopaper_ticks`; a left-over flyer does not count), besides
the 3-tick streak and the game's slip. N3a's desk target is `rulebook_desk_plan`: a `clear_desk_spot` sized to
the OPEN rulebook (226x158 native, both pages; obstacles = stamp bar / tabs / inspect button only, the dropped book
lies on top of papers); a book cut by the booth edge is assumed to extend left; the drag keeps its grab point and
moves by the planned offset (dry 074339 t138: visible 180..314 -> open box 182..408 x 158..316).
A changed ISS. city reading drops the stored verdict (and that tick's), and a city not spelled as in the rulebook
list gets "city 'x' is not in the rulebook for C" in the state block.
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

**Photo check disabled (loop11, 2026-10-03).** `photo_matches_person` is no longer asked (removed from
`manual.INSPECT_KEYS` / `CHECK_KEYS`) and no longer votes in `needed_stamp` (the Day 3 ticket is now `vals[2]`).
Measured reason (private eval, 54 gt-labelled Day 2 entrants, 7 Passport/Face mismatches): AUC 0.31-0.51 across 5
question designs (3-way, binary, 4-attribute, which-differs, where-to-look), and at DENY_P 0.75 it caught 0/7
mismatches while false-denying 4/47 valid entrants (live: Nyyssonen, 164732). A photo mismatch is now approved,
about 1 citation per Day 2, inside the 2 free warnings. The manual's Day 2 rule no longer mentions the photo.

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
- (superseded loop15) ~~Stamped = any of three~~: stamped is now only TOD's `passport_stamp_ink` reading
  (p >= 0.75 after a press, >= 0.85 without one); a press that changed pixels is history only. F2: APPROVED by mistake -> click DENIED (DENIED
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

## `--pause-think` (harness timing feature, off by default; loop18 invisible suspend, 2026-10-03)

**Demo configuration: pause OFF.** The flag is a test-harness aid only (the user judged the suspend visually
unacceptable for the demo); `store_true`, default off, and nothing in `src/`, `tools/` or `deploy/` turns it on.

The game clock is held while TOD thinks. No input is chosen by the harness: no key, no menu, no click -- the
game process is frozen and thawed, nothing else. Per booth tick:
1. grab the frame (game running) -> the only frame extraction and TOD see;
2. `io_win.suspend_game(hwnd)` (NtSuspendProcess on the game pid; a keep-alive thread thaws it 40 ms every 3 s so
   Windows never marks it "Not Responding" -- measured in `pause_and_speed notes` section B,
   `tools/pause_probe.py`);
3. extraction + request 1 / 1b / 2;
4. `io_win.resume_game(pid)`, then TOD's input is executed and the post-wait verification runs (input is never
   sent to a held game: it would queue and land in one burst).
Logged per tick in `tick_NNNN.json` `suspend`: pid, suspend_ms, resume_ms, held_s, thaws (keep-alive count),
keepalive_s, breathe_ms (or `error` when the suspend failed: that tick runs unpaused). A tick that ends early
(skip/stop) and the loop exit resume first; io_win also resumes at process exit. `ThinkSuspend` in loop.py.
The Esc pause-menu path (loop8) is removed.
Reason: Day 3 has 8 entrants and loop17 needed ~45 unpaused ticks of ~20 game-min each -- the day ran out at
18:00 (L1 t47; 1 / 0 / 0 entrants processed before 18:00 in L1-L3); held, a tick costs only the post-wait and animation time.

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
- (superseded loop15: STAMP_INK_Q is now asked via `probe_context.ask_ink`; no E? counter, no stamp hiding)
  Stamp recheck: after `man.UNDECIDED_RECHECK` (3) E? ticks in a row (`Entrant.note_step`), request 1 adds
  `passport_stamp_ink` (approved / denied / none; the yes/no mark question sat at 0.20-0.54 on the inked Uvilia
  passport in the 163640 dry run while this choice read DENIED 0.53-0.84). Ink side p >= 0.75 -> `facts['mark_side']`: stamped from the screen alone (F, or F2 for APPROVED ink with a DENIED verdict),
  stamps hidden otherwise, the entrant offered for the hand-back. One extra question, only on those ticks.

## Put-away spot for flyers / citations (loop23, 2026-10-04)

- The stow target (`stow_papers`, "put-away spot on the desk FOR THE FLYER / CITATION SLIP ONLY") is derived per frame by
  `loop.stow_target` -> `layout.stow_spot`: a patch of the desk sized like the clutter paper, off the counter shelf, the
  tray bar + strips + knob row, both tray tabs, the inspect button, every entrant paper (passport / ticket / unread,
  weight 5) and the planned passport spot; inspector papers (bulletin / rulebook) weight 0.2; far-left / top-left wins.
  Offered only while a flyer / citation is in the way (no fixed counter box any more).
- An executed stow records its spot (`STOWED_SPOTS`); a flyer / citation lying on it is not in the way, an unread desk
  paper on it is `stowed` (never step B / B3, OCR dropped from the request-2 desk text).
- An entrant paper (any doc not a flyer / citation) dropped on the put-away spot is refused (no input, logged).
- loop25 (run 041004 t84-97): the spot keeps `STOW_BAR_GAP` (3 px) below the open bar footprint (TRAY_BAR, excluded in
  both tray states) and is sized at the flyer's full 150x100 (`CLUTTER_SIZE`); a flyer showing >= 85% of its full height
  is not 'cut' by the bar; a paper touches a strip only with >= 3 px vertical overlap; a stowed paper is put away even
  when the open bar overlaps it (only a strip / the passport puts it back in the way); a sliver < 40% of a stowed spot
  does not match it.
- Now-block: K says "Drag THE FLYER itself ... not the paper on the counter shelf"; any step with a flyer / citation
  within 12 native px of an entrant paper adds "drag the passport (the booklet), not the flyer card".
