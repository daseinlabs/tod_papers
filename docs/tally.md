# Tally screen

A third on-screen element for the demo recording, next to the game and the viewer panel: a Papers, Please
style end-of-day ledger with the running counts. It pops up on top at the end of each day, stays up
after the final day, and you can call it up any time.

- Renderer: `src/tod_papers/tally.py`. Window, follow, export and CLI: `tools/tally.py`.
- Like the viewer, it only reads the run directory (`tick_NNNN.json`, then `summary.md` when the run ends)
  and uses `viewer.RunReader` / `list_ticks`. It never talks to the loop or the game.
- The numbers come from `tools/report.py`'s `analyze()`, which tally.py imports, so the two tools always
  agree. `tools/tally.py --run runs/<ts> --numbers` prints them as text.

## What it shows

One row per day (Days 1–3, or whatever the run covers), then a TOTAL row. Above the table, five large
cumulative numbers: correct / judged, citations, processed, TOD decisions, TOD cost.

| column | source |
|---|---|
| SEEN / PROC | entrants who reached the booth (gt entrant segments) / gt `day_processed` |
| APPR / DENY / LEFT | the stamp the game recorded (`gt.given`). LEFT = no stamp (interrogated, sent away, or still at the booth) |
| RIGHT / WRONG / CIT. / SAVED | **ground truth from game memory, not shown to TOD**: `given == correct`, `num_citations`, night-screen `savings` |
| ANSWERS | TOD decisions = questions TOD answered (every request-1/1b state answer plus the request-2 answers) |
| PICKS | executed actions (click / drag / wait; vetoed ticks are not counted) |
| REQ=IMG | TOD requests. Every request carries exactly one image, so this is also the number of images sent. Live: the log's own count; after the run: `summary.md`'s total, split across days by requests |
| COST | **$0.30 per 1,000 decisions**: `cost = rate_per_1k * ANSWERS / 1000`, per day and total (see "Cost model") |
| TICKS / WALL / TICK | ticks, wall-clock from the first tick to the last, median tick period |

Tokens are shown only if a tick record has `tod_usage` / `usage` (`input_tokens`). Current loop runs don't
log this per tick, so the notes line says "not logged". The TOTAL row also counts the title/menu ticks
from before the first day, and the notes line says how many there were. Footer: "every decision = one
image + numbered options -> TOD picks".

## Cost model

- Pricing is **$0.30 per 1,000 decisions**. A decision is one question TOD answered: the ANSWERS column /
  the "TOD DECISIONS" hero number. So `cost = 0.30 * decisions / 1000 = decisions x 0.0003`, per day and
  for the total. `report.py analyze()` computes it (`tick_answers()`, `decision_cost()`); tally.py uses the
  same functions.
- Why "decision" means a question: tod-api's `/v1/systemone` books `decisions=len(questions)` per request in
  its ledger, and the free allowance (`TOD_FREE_DECISIONS`) is counted in that unit. The dollar debit is
  priced separately on input tokens (`TOD_PRICE_PER_MTOK_USD`, placeholder $0.10/M). That is how 684 requests
  in run 115735 came to $0.3133 (about 4.6k tokens per request). Neither "requests" nor "decisions" was the
  unit of that bill.
- The rate and unit are configurable: `--rate-per-1k` (default 0.30, `report.RATE_PER_1K`) and
  `--cost-unit decisions|requests` (default decisions, `report.COST_UNIT`) on both `tools/report.py` and
  `tools/tally.py`. With `requests` the label reads `/ 1k requests` and cost = TOD calls x 0.0003
  (run 182810: 741 -> $0.2223). The cost tile carries the one-line label `cost @ $0.30 / 1k decisions`, and the
  notes line repeats it with the multiplier.
- The same formula applies whether TOD ran on the API or on our own GPU box (where `summary.md` says $0.0000).
  It is exact from the log, so live and finished tallies agree and there is no `~` estimate anymore.
- If `summary.md` has a real API bill (> $0), it appears only as a secondary `billed (API) $x` in the
  notes line, in `--numbers` and in report.py's `cost @ ...` line. Earlier runs divided that bill by
  requests; it is no longer used for COST.

| run | day | decisions | cost @ $0.30/1k |
|---|---|---|---|
| 20261004_182810 | 1 / 2 / 3 / total | 844 / 1,340 / 2,026 / 4,294 (incl. 84 pre-day) | $0.2532 / $0.4020 / $0.6078 / $1.2882 |
| 20261004_175201 | 1 / total | 861 / 947 (incl. 86 pre-day) | $0.2583 / $0.2841 |
| 20261004_115735 | 3 / total | 4,375 / 4,405 (incl. 30 pre-day) | $1.3125 / $1.3215 (billed: $0.3133) |

## When it pops up (`--follow`)

The window starts hidden. When it shows, it is topmost, has no taskbar button, and never takes focus
(`WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST`, shown with `SetWindowPos(HWND_TOPMOST, SWP_NOACTIVATE | SWP_SHOWWINDOW)`).
By default it is also click-through (`WS_EX_LAYERED | WS_EX_TRANSPARENT`), so if the loop clicks under it,
the click reaches the game. The window title is `TOD tally`.

Day end is detected from the log only (`tally.day_end_events`):
1. the first tick where gt shows `screen == "NightScreen"` for day d, or
2. if that night was never logged, the first tick of day d+1 (gt `day` increments), or
3. if gt is absent altogether, TOD's own `state.screen == day_end` (p >= 0.5) on two ticks in a row.

- Days before `--final-day` (default 3): the tally shows for `--secs` seconds (default 12).
- The `--final-day` day end, or the run finishing (`summary.md` appears): the FINAL tally, which stays up.
  When `summary.md` arrives it is repainted with the exact request count (the cost does not change).
- **F9** (global, polled) toggles the tally for `--secs`. `tools/tally.py --show-now [SECS]` writes
  `runs/.tally_show_now`, and the running window shows itself for SECS. This does nothing while the final
  screen is up.
- Day ends that are already in the log when the tool attaches don't trigger a popup (use
  `--fire-existing` to change that). With `--follow` it attaches to the newest `runs/2*` and switches to newer ones.

**Capture caveat:** the loop grabs the game's client area from the screen (dxcam/mss). If the tally covers
the game while the loop is still playing (the Day 1 and Day 2 night screens), TOD's frames for those
ticks show the tally. Either keep `--secs` short, or place the tally over the panel area with `--x/--y`
for the Day 1–2 pops. The final popup happens after the loop has stopped (`--stop-on-gt-day-end 3`), so
it can't affect anything.

## `tally_schedule.json`

Written to `runs/<ts>/` in follow mode and export mode. It is atomically rewritten (tmp + `os.replace`)
every time the window is shown or hidden. The Cap zoom-plan tool reads it to add "tally" beats.

```json
[
 {"start": "2026-10-03T16:50:44.943", "end": "2026-10-03T16:50:56.943", "reason": "day_end day 1 tick 34: gt NightScreen -> tally_day1.png"},
 {"start": "2026-10-03T16:59:31.431", "end": null, "reason": "final day 2 tick 121: run finished -> tally_final.png"}
]
```

- A JSON **list**, with one entry appended each time the window is shown.
- `start` / `end`: local wall-clock ISO time (`YYYY-MM-DDTHH:MM:SS.mmm`). `end` is `null` for the final
  screen that stays up, or while an entry is still open.
- `reason`: free text, `"<kind> day <d> tick <n>: <why>"`. kind is `day_end`, `final`, `run_end` or `manual`.
  In export mode it ends with ` -> <png>`.
- Follow mode appends to an existing file. Export mode replaces it, using tick times: a day end runs
  from start to start + `--secs`, and the final entry has end null. If you re-export a recorded run, back up
  the live `tally_schedule.json` first and restore it afterwards. That file is the record of what the video showed.

## Export = what the live window drew

`--export` renders each tally the way the live window did. A day-end tally uses only the ticks up to that
point and no `summary.md` (it didn't exist yet), so REQ=IMG is the log's own count. The final tally uses
the finished run. While a day-end tally is up, the live window repaints on every new tick (the header tick
and the first row of the next day change). For that reason, export also writes
`tally_day<d>_t<NNNN>.png`, one per tick loaded while it was up. `tools/patch_tally.py` uses these files
to re-skin a recorded video (docs/demo.md, "Patching the tally in a finished video").

## Commands

```powershell
# live demo: start BEFORE the loop (centred on the primary display; 3456x2160 here)
.venv-loop\Scripts\python.exe tools\tally.py --follow --secs 12 --width 1920 --height 1080
#   manual pop from another shell (or press F9):
.venv-loop\Scripts\python.exe tools\tally.py --show-now 8

# export per-day + final PNGs (runs/<ts>/tally/) and tally_schedule.json, for the time-lapse fallback
.venv-loop\Scripts\python.exe tools\tally.py --run runs\20261004_115735 --export
# one frame as of tick N
.venv-loop\Scripts\python.exe tools\tally.py --run runs\20261004_115735 --tick 120 --out tally_t120.png
# numbers as text (compare with tools\report.py runs\<ts>)
.venv-loop\Scripts\python.exe tools\tally.py --run runs\20261004_115735 --numbers
```

Flags: `--secs` (12), `--final-day` (3), `--rate-per-1k` (0.30), `--width/--height` (1920x1080, rendered to fit and letterboxed),
`--x/--y` (physical px, default centred), `--poll` (0.25 s), `--no-hotkey`, `--no-click-through`,
`--fire-existing`, `--run` (follow one specific run instead of the newest).
