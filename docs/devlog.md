# Development log

A factual chronology of how the Papers, Please loop got from "TOD clicks a menu button" to
"TOD clears Day 3". Every number below comes from a run folder (`runs/<id>/tick_NNNN.json`,
`summary.md`); verdict correctness is scored with the game-memory reader (`gt.py`), which is
never shown to TOD. Runs are named by their start time (`YYYYMMDD_HHMMSS`).

Loop iterations are numbered loop1 ... loop32; each one is a batch of code changes followed by
offline dry runs on saved frames and (usually) up to three live launches.

## Phase 1 -- menus, and why empty options fail (2026-10-01/02)

**Setup.** One request per tick: the Set-of-Mark (SoM) frame, every detected box as a numbered
choice, and the questions `action` (click / drag / wait), `source`, `target`, `screen`, `goal`.

- **Menus work zero-shot.** STORY (p 0.81) -> Day 1 tile -> NEXT through the intro -> WALK TO WORK
  (0.79) -> booth. No scripting; extraction -> SoM -> TOD -> click.
- **The booth did not work** (run `20261001_221705`, 40 ticks): TOD never dragged or stamped,
  called the booth a `menu`, and the source distribution was flat (0.05-0.2 per option).
  Cause: the option texts were `icon -- (no text)`. A number with no description carries no
  information TOD can match against the image.
- **Fix: describe every box.** Option text became `<kind> -- '<ocr text>' (<position>)` or
  `<kind> -- <open-vocabulary label> (<position>)`. OCR moved to PP-OCRv5 on 3x line crops
  (it reads DENIED, ALIGN VISA BENEATH STAMP, NEXT, Arstotzka; PP-OCRv3 and native-1x OCR did
  not), and Grounding DINO was added to label untexted boxes ("lever handle", "rubber stamp",
  "speaker/horn", "passport booklet", "folded page corner", ...). Idle-booth dry run: TOD's top
  pick went from the bulletin at ~0.2 to the loudspeaker horn at 0.71-0.75.

## Phase 2 -- state as its own request (loop3-loop5)

- **State questions inside the long action prompt were near-uniform.** Asking `screen` as a
  separate short request on the *unmarked* frame fixed it, and it runs in parallel with
  extraction, so it adds no latency. The tick became **two requests, one image each**:
  request 1 (state, raw frame) and request 2 (action, SoM frame).
- Conditioning the source question on the state answer lifted the horn from ~0.01 to ~0.7 once
  the screen was read correctly; capping the options at 25 added another 0.05-0.10.
- **Missing boxes are fatal.** The passport lying on the counter got no box (YOLO and the panel
  finder missed it, Grounding DINO's best label was "rubber stamp" at 0.25), so TOD picked the
  rulebook. A counter-strip Grounding DINO pass and CLIP labels for closed/open passport and
  rulebook fixed detection.
- **Live run `20261002_003519`** (60 ticks): TOD opened the stamp tray by itself (p 0.21) and
  clicked APPROVED -- with the visa above the tray, not under the stamp. Every drop went to one
  generic "empty space" target that sat under the open tray. Lesson: drop targets must be the
  places that matter in the game. The generic target was replaced by **derived drop targets**:
  the two stamp landing strips (computed from the detected stamp heads), the counter, later the
  entrant himself.
- **Hand-back = drop the papers on the person.** Dropping them on the counter shelf does
  nothing; dropping them on the entrant in the window returns them ("Thank you."). A derived
  `hand_back` region from the largest person box became a target.
- **First full entrant:** run `20261002_101448` -- APPROVED pressed, passport dragged onto the
  entrant, entrant leaves, next entrant called.

## Phase 3 -- heuristics replaced by TOD questions (loop6, 2026-10-02 afternoon)

Up to here several facts were decided in code with regexes and pixel tests (issuing country
from OCR tokens, "is the passport open" from field regexes, stamp ink from colour, which paper
is the passport). The rule adopted: **no keyword or pixel judgement where a question to TOD can
answer it.**

- Country: a choice over the 7 countries plus `unreadable`.
- Passport readable: a yes/no question (`passport_open_readable`).
- Paper under each stamp: `passport_under_denied` / `passport_under_approved`.
- Stamp ink: `passport_shows_stamp_mark`, plus the recorded press.
- **Per-paper identity:** one question per paper box -- "what is this paper?" over passport /
  rulebook / bulletin / entry_ticket / transcript / flyer / citation / other, with the box's OCR
  text in the question. The answer goes into that element's option text
  (`passport (TOD 0.48) -- <position>`).
- Remaining regex: only the delete/trash safety veto on menu screens.

Run `20261002_114927` had shown why: with the old gates the country was never read and the
rulebook, dragged under the DENIED strip, was stamped 9 times. After the change a dry run on 14
such frames had TOD pick a stamp 0 times. First loop6 live run (`20261003_005956`): 2 entrants
correct and handed back, the third stalled.

## Phase 4 -- Day 1 cleared; Day 2 readings (loop7-loop8, 2026-10-03)

- **Day 1 cleared twice, 4/4 by ground truth** (`20261003_033604`, `20261003_054238`).
- **Day 2 readings as choices over OCR candidates.** Asking TOD "is the passport expired?"
  misread about 1 in 3 foreigners. Instead, request 1b asks *which* of the dates OCR found is the
  one after `EXP.`, and which OCR token is the ISS. city; the comparison with today's date and the
  rulebook city list is the rule, stated in text. Offline evaluation on 30 passports: expiry
  3/30 -> 28/30, city 26/30 -> 29/30, 0 wrong.
- **Clock vs. tick time.** A day is about 3-4 real minutes of game clock; a tick took 6-17 s and
  an entrant 6-20 ticks. Entrants processed after 18:00 are unpaid, and two runs ended Day 2 in
  debt (game over). `--pause-think` was added: the harness suspends the game process while TOD
  thinks and resumes it afterwards (no input is sent; the frame TOD sees is the frame before the
  suspend). With it, Day 1 processed 8 entrants by 17:40 instead of 4-5.
- **Day 3 reached** in run `20261003_092642` (Day 1 7/7, Day 2 4/5 decided correctly).

## Phase 5 -- the verdict becomes TOD's question (loop14-loop20)

An audit of every place where code (rather than TOD) could choose an input found that the
verdict itself was implicit: code hid the stamp that did not match the rule. Fixed in loop15:

- **`verdict` is a request-1b choice** (approved / denied / cannot_decide_yet). Its text states
  today's rule and lists TOD's own earlier readings of this entrant's papers, with the tick and p
  of each. Code does not apply the rule.
- **Stamp offered = the stamp TOD's verdict names.** The other stamp is struck from the SoM frame
  and logged as `stamp_hidden: hidden_by: tod_verdict=<side> p=<p>`.
- **Press gate:** a stamp press is executed only if TOD's own strip answer says the passport is
  under that stamp; otherwise it is refused (no input) and the refusal is shown next tick.
- Loop17-19 live runs showed the failure mode this guards against: TOD pressed the stamp the
  passport lay under rather than the one its verdict named. With only the verdict's stamp offered
  (loop20, fix 48d3519): 8/8 stamps correct across three launches, 0 wrong-stamp presses.
- **Day 3 entry ticket:** first a yes/no question (wrong denials when the ticket was still on
  the counter), then a 4-way choice with `no_ticket`, then a choice over the OCR'd `VALID ON`
  lines only (taking every desk date let TOD pick the passport's EXP. date). A reading taken while
  a paper still lies unread on the counter is shown as "not read" so the verdict says
  `cannot_decide_yet`.

## Phase 6 -- manual bloat -> rules + dynamic block (loop22)

The request-2 text had grown to a 17k-character prose script of ~25 lettered steps; at 29k
characters TOD returned HTTP 413 `state_too_long` (run `20261004_004042`, t115).

- The manual was rewritten as **rules only** (~2.4-2.5k characters: what is where, how papers
  move, today's rules, the verdict procedure).
- A per-tick **dynamic block** ("WHAT APPLIES NOW (from your own answers above)") states the one
  or two facts that matter this tick, each derived from TOD's own answers, e.g. "Your verdict
  (this tick): APPROVED p=0.77. The passport lies under APPROVED: press the APPROVED stamp once."
- History shortened to the last 15 actions. Request-2 text dropped to 4.5-6.9k characters.

## Phase 7 -- clutter (loop23-loop27)

From Day 2 on, the desk fills with things that are not the entrant's papers: the Pink Vice
flyer, M.O.A. citation slips, the rulebook. Failure modes seen live: a citation merged into the
passport box and handed back as the passport; flyers under the open stamp bar blocking the
press; flyers dragged back and forth between counter and desk.

- `flyer`, `citation` and `game_slip` paper kinds in the identity question.
- A **put-away spot** derived per frame on the desk (below the stamp bar when the tray is open);
  stowed papers are struck from the options until they are in the way again.
- Paper sheets are segmented so the passport and an overlapping ticket come back as separate
  boxes, each with its own OCR text.
- Run `20261004_031627`: 7/7 verdicts correct, 0 citations, before the first no-documents entrant.

## Phase 8 -- the no-documents entrant (loop23-loop32)

Day 3's eighth entrant (Jorji Costava) brings no papers. The game expects the inspector to open
inspect mode, select the rulebook line "Entrant must have a passport", select the empty counter
(DISCREPANCY DETECTED), then interrogate.

- `no_documents_presented` became a request-1 question; step N starts when TOD answers yes on
  several recent ticks with nobody's papers named.
- The rulebook page (`rulebook_page`) is a TOD question; the contents entry 'Basic Rules' and the
  page corner are offered from OCR and a template match.
- Inspect mode dims the booth, which lowered TOD's `person_at_window` and dropped step N every
  other tick (run `20261004_070003`); the threshold is lowered while inspect mode is on.
- The rule line is re-read by local OCR when the remote extractor returns it inside a page box.
- The empty counter, the inspect button and the microphone (INTERROGATE) became static layout
  fixtures, offered only in the matching sub-step of N.
- **Run `20261004_115735`:** rulebook to desk (t132), Basic Rules (t133), inspect button (t134),
  rule line (t135), empty counter (t136), microphone (t138) -> "Where is your passport?" (t139);
  he left on his own, no stamp, no citation. Same run reached Day 3's end (NightScreen) with 10
  entrants processed and 3 citations.

## What did not work, in one list

| tried | result | replaced by |
|---|---|---|
| options without descriptions | flat probabilities, wrong screen | OCR + open-vocab label + position in every option |
| state questions inside the action prompt | near-uniform answers | separate state request on the raw frame |
| one generic "empty space" drop target | papers dropped under the tray | derived targets: stamp strips, entrant, put-away spot |
| regex / pixel tests for country, open passport, ink | gates never fired; rulebook stamped 9x | TOD questions |
| "is it expired?" yes/no | ~1 in 3 foreigners misread | choice over the OCR'd dates |
| code hides the wrong stamp | the rule was applied by code | `verdict` question; stamp shown = TOD's verdict |
| 17-29k-character prose manual | HTTP 413, slower requests | 2.4k-char rules + per-tick dynamic block |

## Open issues (as of loop32)

- Photo check: a confident `different` is required to deny, so look-alike photos are approved
  (one Day 3 citation, one or two per Day 2 run).
- OCR misreads on the pixel font (`Eist Grestin`, `1943.11.25`) become wrong readings.
- After the interrogation dialogue starts, step N re-runs for ~8 ticks (harmless, wasted ticks).
- Without `--pause-think` the game clock outruns the tick time on Days 2-3.
