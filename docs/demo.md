# Demo run: TOD plays Papers, Please Days 1-3 in one go

`tools/demo_run.py` runs the whole demo from a cold game start. It starts at the title screen, with the story
save wiped so DAY 1 is the only tile ("NEW"). From there it plays Day 1, night, Day 2, night, Day 3 and stops at
the Day 3 night screen, all in ONE `tod_papers.loop` invocation. Every click from the title screen onward is TOD's
pick. That includes STORY, the DAY tile, CONTINUE, NEXT, WALK TO WORK and SLEEP: these are OCR'd options on the
non-booth screens (manual step 7).

## Commands

Run all of these from the repo root with the loop venv:

```
# the recorded demo take (OBS: game + TOD sidebar + tally, one continuous MP4; docs/recording.md)
python tools/produce_demo.py --days 1

# the loop alone, no recording: fresh story, game clock running while TOD thinks (default)
.venv-loop\Scripts\python.exe tools\demo_run.py

# same, with the game process held while TOD thinks (slower wall-clock, no 18:00 pressure)
.venv-loop\Scripts\python.exe tools\demo_run.py --pause-think

# retake from day N: saves kept (backed up anyway), TOD picks the day tile, still ends at the Day 3 night
.venv-loop\Scripts\python.exe tools\demo_run.py --from-day 3

# per-day / per-entrant report of any run folder (demo_run prints it at the end too)
.venv-loop\Scripts\python.exe tools\report.py runs\<run_dir>
```

To stop cleanly mid-run, use `type nul > runs\STOP_LOOP`. Do not use Ctrl-C under `--pause-think`: the stop
file makes sure the game is resumed. Other useful flags are `--local-extract` (no tunnel), `--no-launch` (the game
is already on the title screen), `--max-ticks` (default 1500) and `--loop-args "..."` (extra loop flags).

### Optional: a faster TOD scorer on your own GPU (`--fast`)

By default the loop scores on the public API (`https://tod.parseclab.ai/v1/systemone`). `src/tod_papers/tod_client.py`
reads **`TOD_API_URL`** (a base URL). Point it at any server that speaks the same `/v1/systemone` request and
response shape.

`deploy/gcp_fast.sh` runs that kind of server on one A100 40GB spot VM:

- It uses the TOD picker with its shared-prefix scorer, which encodes the screenshot and state once per request
  instead of once per question.
- `deploy/fast_shim.py` puts the public request/response shape in front of it.
- It is reachable only through an SSH tunnel on `127.0.0.1:8790`, so it needs no API key.
- The image and weights come from our private registry and bucket, so outside our project the script is a recipe
  only. The script takes `PROJECT` from the environment or `gcloud config`, and the weights bucket from
  `WEIGHTS_BUCKET=gs://...` (optionally `IMAGE=...`).

```
bash deploy/gcp_fast.sh start      # create/resume the VM (spot A100, roughly $1.2/h while running)
.venv-loop\Scripts\python.exe tools\demo_run.py --fast    # opens the tunnel, checks /health, sets TOD_API_URL
bash deploy/gcp_fast.sh stop       # stop billing when done
```

Measured on 2026-10-04 by replaying 5 requests from a stored run (`tools/fast_replay.py`):

| | 13-question state request | 3-question action request |
|---|---|---|
| speedup, same A100 (reference scorer vs shared-prefix) | about 2.7 to 2.9x | about 2x |
| speedup, through the tunnel vs the public API | about 1.8 to 2.1x (≈1.75 s → ≈0.9 s) | about 1.4x (≈1.7 s → ≈1.2 s) |

- Probabilities match the public API to within the GPU-to-GPU noise. The largest per-option difference was 0.13.
  That was on a near-tie question, where the reference scorer on the A100 itself differs from the API by 0.28.
- In a live title-screen tick, the two TOD requests took 592 ms and 465 ms, against 897 ms and 881 ms on the API.

What `demo_run.py` does:

1. It kills the game and copies `%APPDATA%\3909\PapersPlease` to `runs\save_backup_<ts>\`. Only with
   `--wipe-saves` does it delete the story progress files, using the same list as `tools/reset_game.py`. It removes `runs\LAST_DAY.json` so that no day reading carries into the run.
   **Saves are kept by default (2026-10-04).** TOD starts a new story from the game's own menu instead.
   Deleting the progress files does not survive a launch: Steam Auto-Cloud downloads them back (run
   20261004_145643 came back to the Day 3 tile). A `cloud > "enabled" "0"` key in `localconfig.vdf` did not stop
   that either. `--wipe-saves` keeps the old delete as an opt-in. Without `--from-day`, the loop runs with
   `--new-story`. On the menu and day-select screens, TOD's text then carries the line "GOAL: start a NEW story
   from Day 1 (do not continue an existing save)", plus a note that the DAY 1 tile is labelled NEW. The
   other-screens manual says to follow a GOAL line, and to take the HIGHEST tile only when there is none. All
   tiles stay offered as options, and TOD chooses. With saves present, the day-select screen shows DAY 1 "NEW",
   then one dated tile per saved day (the latest marked in red). Clicking NEW goes straight to the Day 1 intro,
   with no confirmation box.
2. It relaunches the game through Steam (`steam://rungameid/239030`), waits for the window and for a still frame,
   and runs `io_win.ensure_onscreen`. It logs the client rect.
3. It checks that game-memory ground truth (`tod_papers.gt`) attaches.
4. It picks a remote extractor, A100 first: `GET http://127.0.0.1:8766/health`; if down it starts
   `deploy/gcp_l4_spot.sh tunnel --a100` detached (log `runs\tunnel_<ts>_a100.log`) and waits for
   `/health`. If the A100 does not answer (VM stopped, ssh exits, or `--tunnel-wait` runs out), it falls back
   to the L4: `127.0.0.1:8765`, `deploy/gcp_l4_spot.sh tunnel` (log `runs\tunnel_<ts>_l4.log`). The one used
   is logged ("using the A100 extractor at ...") and stored as `extractor` in `runs\demo_<ts>.json`. It never
   starts or stops an instance. `--extract-url URL` skips the choice and uses only that URL.
   `TOD_EXTRACT_URL` is set only in the loop's environment. It does not start the VM: if `/health` never answers,
   run `deploy/gcp_l4_spot.sh start` yourself.
5. It runs `python -m tod_papers.loop --max-ticks 1500 --save-raw --stop-on-gt-day-end 3 --stall-stop 12
   --pick-stop 10 --refuse-stop 5 [--pause-think]`.
6. It writes `runs\demo_<ts>.json` (window rect, loop command, stop reason, wall time) and prints the report.
   Exit codes: 0 means the Day 3 night was reached, 1 is a launch/gt/tunnel failure, 4 means the loop stopped
   anywhere else.

### The stop: `--stop-on-gt-day-end 3` (ground truth for the STOP only)

The loop stops on the first tick where gt (game memory) reports `day >= 3` and `screen == NightScreen`. It stops
before any TOD request on that tick and sends no input. gt decides only when the harness stops. It is never in
TOD's text, and it never influences a TOD answer or pick.

`--stop-on-screen day_end` is not used because it would stop at Day 1's night. TOD's `day_end` answer also does
not say which day ended. In run 182519, TOD's top screen answer for the Day 3 date card (t86) was `day_end`, at
p 0.30. That is below the 0.5 `STOP_SCREEN_P` threshold, so it would not have stopped that run.
`demo_run.py` asserts that the flag is absent.

On run 20261004_115735, the gt stop would have fired at t252, the first Day 3 NightScreen tick. That is one tick
before the `day_end x2` stop that actually ended the run. On 20261003_182519 (Day 1 to 3) and 20261003_164732
(Days 1-2), it never fires, including on the Day 1 and Day 2 nights.

### Non-booth screens: no stop rule fires (checked in `loop.py`)

Night, news, bulletin, day-select, menu and cutscene screens map to manual step 7. On step-7 ticks the loop does
the following:

- The `--stall-stop` key counter is reset (`stall_n = 1`).
- `--pick-stop` is reset.
- The repeat-input stop (`REPEAT_STOP`) is skipped.
- The cycle guard is not fed and its stop is skipped.
- `--refuse-stop` counts only refused stamp presses, and there are no stamps on these screens.

The only stop that applies is `NONBOOTH_STOP = 60` step-7 ticks in a row. In 182519, each night-to-booth
transition took 4 step-7 ticks: t25-28 and t82-85, with booth frames again from t29 / t86.

## Expected durations

The figures below are from runs 182519 (un-paused) and 115735 (paused):

| mode | ticks / day | entrants / day | tick | Days 1-3 |
|---|---|---|---|---|
| un-paused (default) | ~38 (the game clock reaches 18:00; 182519: Day 1 27 from a mid-day resume, Day 2 57) | ~4 (182519: Day 2 6) | ~6.5 s (182519 median 6.2 s) | ~13-15 min of loop + ~1-2 min launch/menus |
| `--pause-think` | ~250 | 10-13 (Day 3: 13 seen, 10 processed in 115735) | ~6 s (median 5.1 s) | ~75-80 min |

Un-paused, the game clock runs during TOD's ~4-6 s of thinking, so each day ends at 18:00 after ~4 entrants. Under
`--pause-think`, the clock is held, so a day runs until the game's own quota/clock ends it.

## What the recording should frame

`io_win.ensure_onscreen` only moves the window if part of the client is off the virtual desktop. Otherwise the
window stays where Steam opened it. The client is expected to be 2280x1280 physical px (4x the native 570x320).

`demo_run.py` logs `client (x,y,w,h physical px) = [...]` and the screen size before the loop starts, and saves
them as `client` / `screen` in `runs\demo_<ts>.json`. The recording does not depend on that rect: OBS captures
the game window itself (docs/recording.md).

Keep other windows off the game: the loop waits, without input, while the game is not in the foreground, and
aborts after `--fg-patience` (60 x 2 s). The cursor parks at the top-right corner of the client between actions.
The TOD overlay (`overlay.py`) is a separate window; frame it too if it should be in the recording.

## Honest caveats

- **Known verdict-miss classes.** These are from the loop32 Day 3 run 115735 (3 citations in 10 processed):
  - The issuing-city reading (Hinchliff: gt Passport/IssuingCity, TOD APPROVED).
  - Face/fingerprint mismatches that the TOD verdict does not catch (Medici: gt Passport/Face +
    IdentityRecord/Fingerprints). The demo does not compare photos or fingerprints.
  - Date OCR misses on the expiry (Kovacs: expiry read 1943.11.25, so DENIED a valid entrant).

  On Days 1-2 (run 164732), the Day 2 misses were 2 of 5.
- **Jorji Costava** (the no-passport entrant, then the fake-passport interrogation) appears only if 8 or more
  entrants are processed on Day 3. Un-paused (~4 entrants/day), he does not appear. Under `--pause-think` he
  appeared in 115735 as the 8th Day 3 entrant: TOD interrogated him and he left without a stamp. That is correct
  behaviour, and he is listed as `no stamp` in the report.
- **Day 2 ends at the bombing** mid-entrant. The entrant on the desk at that moment shows as `no stamp`.
- Un-paused, the first Day 1 entrant often arrives with the clock already moving. Entrants still in the booth at
  18:00 are not processed.
- `--from-day N` cannot steer TOD to a lower day. The manual tells TOD to pick the HIGHEST day tile, so a retake
  of day N needs saves whose highest day is N. `demo_run.py` warns if the first booth day differs.
- Ticks are not deterministic. TOD's answers vary run to run, and a stall/cycle/refuse stop can still end a run
  early on a booth screen (exit 4). The report shows where.
- Ground truth (gt) is used for the stop and the report only.

## `tools/report.py`

`tools/report.py` reads the tick JSONs and prints three kinds of output:

- **Per entrant:** name, TOD's verdict answer (request 1b) and its p on the first stamp-press tick, stamp side(s)
  pressed, gt correct / given verdict, whether they match, ticks, and whether a citation followed.
- **Per day:** entrants processed (gt), correct/wrong, citations, savings on the night screen, ticks, TOD calls
  (apportioned by the requests made on that day's ticks), decisions and $.
- **Run totals:** wall-clock, median tick, median ms for extract / request 1 / 1b / request 2, and pause on/off.
- **Cost:** **$0.30 per 1,000 decisions**, where a decision is one question TOD answered:
  `cost = decisions x 0.0003`. This holds per day and in total, on the API or on the `--fast` GPU box alike.
  Change the rate with `--rate-per-1k R` on `tools/report.py` and `tools/tally.py`. A real API bill from
  `summary.md` is printed only as a secondary `billed` figure. See docs/tally.md "Cost model".

Runs before loop15 have no request-1b verdict answer, so that column shows `(not logged)`.

## Patching the tally in a finished video (no retake)

Use this when the tally's numbers or wording changed after the take (for example the cost model). Each
TALLY span from `scene_log.json` is re-skinned with re-exported PNGs, and the rest stays as recorded.

```
# 1. corrected PNGs, rendered exactly as live (day ends: the per-tick repaints tally_day<d>_t<NNNN>.png)
cp runs/<ts>/tally_schedule.json runs/<ts>/tally_schedule.live.json
.venv-loop\Scripts\python.exe tools\tally.py --run runs\<ts> --export
cp runs/<ts>/tally_schedule.live.json runs/<ts>/tally_schedule.json
# 2. patch: matches every TALLY-span frame to its PNG variant (cost regions masked), tracks the ~0.7 s Move
#    slide in/out per frame, overlays the PNGs scaled 1920x1080 -> 3840x2160 (the OBS TALLY scene: bounds =
#    full canvas, scale 2.0, lanczos), h264 QP 18 (NVENC if it opens, else libx264 -preset veryfast:
#    the winget ffmpeg 9 build needs NVIDIA driver >= 610), audio + chapters stream-copied
.venv-loop\Scripts\python.exe tools\patch_tally.py --run runs\<ts>     # -> runs/<ts>/demo_day3_final.mp4
#    --dry-run prints the spans / segments and the ffmpeg command (also in runs/<ts>/tally/patch_plan.json)
```

It aborts if a static frame inside a TALLY span matches none of the PNGs, which means the PNGs differ
from the live render somewhere other than the cost regions.

## Speed versions

```
ffmpeg -i runs/<ts>/demo_day3_final.mp4 -filter_complex "[0:v]setpts=PTS/2,fps=30[v];[0:a]atempo=2.0[a]" ^
  -map "[v]" -map "[a]" -c:v h264_nvenc -preset p5 -tune hq -rc constqp -qp 18 -profile:v high -pix_fmt yuv420p ^
  -c:a aac -b:a 192k -movflags +faststart runs/<ts>/demo_day3_final_2x.mp4
# 3x: setpts=PTS/3, atempo=3.0 -> demo_day3_final_3x.mp4
# NVENC fails to open ("Required: 13.1 Found: 13.0") with a driver older than 610: use
#   -c:v libx264 -preset veryfast -qp 18 -profile:v high   instead of the h264_nvenc options
```

`atempo` changes speed and keeps the pitch. `fps=30` drops the extra frames so the output stays 30 fps CFR.

## Offline sanity check (2026-10-04)

`loop.py --frames ... --sequential` was replayed on run 182519 for t0-15 (Day 1 booth), t24-31 (Day 1 night,
news, Day 2 booth) and t81-88 (Day 2 night, news, Day 3 booth). It cost 3 TOD calls per frame, about $0.05.

- No stop or cycle rule fired.
- Every non-booth tick was step 7, and TOD's picks matched the live run: SLEEP, then screen, then WALK TO WORK,
  then screen.
- On booth ticks the offline picks differ in places, because the replay has no live effect checks. Examples:
  t29/87/88 offline drag the counter document where the live run clicked the horn, and t12 offline waits.
  These differences are on booth screens, which `--stop-on-gt-day-end` does not affect.
