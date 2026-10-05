# Behind-the-scenes viewer (for screen recording)

`tools/viewer.py` opens a dark, tall panel window that shows, per tick, what TOD was given and what it
answered. It sits **beside** the game window, never over it: the loop captures the game's own window, so
TOD always gets an unmodified game frame. The viewer only polls the run directory (read-only); it does not
import loop code, talk to TOD, or touch the game, so it has no effect on a running loop. Renderer:
`src/tod_papers/viewer.py` (PIL image, shown by a Tk window; same code for live, replay and PNG export).

## Commands

```
python tools/viewer.py --follow                         # live: newest runs/<ts>, switches to a newer run
python tools/viewer.py --run runs/<ts>                  # live on one run dir
python tools/viewer.py --run runs/<ts> --replay --fps 2 [--start 130 --end 140]
python tools/viewer.py --run runs/<ts> --export out/ [--start N --end M]   # view_NNNN.png, no window
```

Use `.venv-loop\Scripts\python.exe`. Keys: space pause/resume, Left/Right step (pauses), Home/End,
q/Esc quit. `--width/--height` (debug default 1160x1980 physical px), `--x/--y` (default: right of the game
window, i.e. the `UnityWndClass` window titled "PapersPlease", the loop's own match, top aligned; else
x=2290, y=0). Two layouts: `--layout debug` (default, everything, described under "Panel") and
`--layout video` (the demo sidebar, see "Video layout").

The viewer process runs at below-normal priority and only reads files the loop has already written (plain
`open(..., "r")` / `cv2.imread`, no locks, no renames), so it cannot block or slow the loop's writes; a
half-written json fails to parse and is retried on the next poll.

Live mode shows a tick once its `tick_NNNN.png` exists (written on the loop's log thread right after the
json), or once the json is 3 s old. A panel renders in ~0.35 s.

## Panel (top to bottom)

1. **What TOD was shown**: `tick_NNNN.png`, the numbered SoM frame of request 2 (shrinks if the text needs room).
2. **What TOD sees**: request-1 answers from `state` with p (screen, person, counter, desk, tray, passport
   under which stamp, ink, country, EXP., ISS. city, ticket, no papers, inspect, interrogate, rulebook page);
   amber = value changed since the previous tick. Below: paper identities (`docs_named`).
3. **Situation sentence**: the `WHAT APPLIES NOW` block of `state_text` (the dynamic sentence sent with
   request 2) and today's rule line from the manual.
4. **TOD's verdict**: `state.verdict` probs as one stacked bar; "stored" when only the entrant memory has it.
5. **TOD's pick**: top-3 `answers.source` options with p and option text (`descriptions`), action probs, drop
   target for a drag, `executed` (red when vetoed), conventions/exclusions/cycle breaks if any.
6. **Last 8 actions**: green = screen changed after the input, red = no change, grey = not checked.
7. **Footer**: game memory ground truth (`gt`: day, clock, processed, citations, entrant's correct verdict),
   labelled "not shown to TOD"; tick period and its split (vision extract, TOD state, papers, TOD action).

## Layout on this desktop (3456x2160, 225 % scaling)

The game client is 2280x1280; with the window at the left edge (x=0) there are ~1170 physical px to its
right, so the 1160-px-wide panel fits next to it with nothing overlapping. Start the viewer **before** the
loop (a new Tk window takes the foreground once at start; the loop's focus guard then keeps the game in
front, and the viewer never asks for focus again). Do not drag the viewer over the game window.

Recording suggestion: record the full desktop (OBS display capture, output 1920x1080, or 3840x2160 for a
crisper panel). Left ~2/3 = live game, right 1/3 = panel. At 1080p output the panel's body text is
~15 px high, readable; the SoM frame is a small version of the game, so the numbered tags show which option
TOD picked. For a video without a live game: `--replay --fps 1` (or `--export` the PNGs and edit them next
to the recorded `raw_NNNN.png` frames).

## Video layout (`--layout video`, for the demo recording)

```
python tools/viewer.py --follow --layout video          # live sidebar next to the game, animated
python tools/viewer.py --run runs/<ts> --layout video --replay --fps 2 [--start 120 --end 150]
python tools/viewer.py --run runs/<ts> --layout video --export out/        # panel PNGs (final state per tick)
python tools/viewer.py --run runs/<ts> --layout video --export-video out.mp4 [--hold 0.5] [--start N --end M]
        [--video-fps 30] [--scale 1.0] [--panel-only]
```

Renderer: `src/tod_papers/viewer_video.py`. Recording with Cap: one display, game + panel side by side,
the editor zooms in on the game and out to game + panel.

**Geometry.** Display 3456x2160 physical (225 %); the game runs borderless at 2280x1280 client px at (0, 0).
The panel takes the rest of the width to the right of the game: by default **1176x1944 at (2280, 0)**, three
stacked 16:9 tiles of 1176x648 that the Cap zoom plan (`tools/cap_zoom_plan.py`, docs/recording.md) zooms
onto at amount 2.939 and pans between:

```
tile 1  (2280,    0, 1176, 648)
tile 2  (2280,  648, 1176, 648)
tile 3  (2280, 1296, 1176, 648)
```

(`viewer_video.tile_rects()` returns these.) In `--follow` the window is borderless (`overrideredirect`), placed
right of the game's client rect, top-aligned; with no game window it uses the right 1176 px of the display,
y=0. `--width/--height/--x/--y` override: `--height 1280` (game height, one strip with the game) and
`--height 2160` (full column) still give the earlier single-flow layouts described further down. Keys need
one click on the panel first (borderless windows get no focus on their own; the loop's focus guard keeps
the game in front anyway).

**Same content as `--layout debug`, restyled for video.** Nothing from the debug panel is dropped. Body text
is 22-26 px (22 px small/notes, 23 px body, 26 px title). Every section has a fixed slot, so nothing jumps
between ticks.

**Three-tile layout (default, 1176x1944).** Hard separator lines at y=648 and y=1296. Every tile reads as a
complete slide when the zoom shows it alone, and nothing crosses a boundary:

* **Tile 1 (y 0-648)**:
  * header: title, run, tick counter
  * **"What TOD was shown · request 2"**: the numbered SoM still (776x436) with the amber action marker
    (ring at the click / grab point, arrow to the drop point)
  * beside the still:
    * **TOD's verdict**: approve / deny / cannot decide yet as 3 labelled bars with p ("stored" for a memory
      verdict)
    * **TOD's pick**: `#id verb`, its p, the object description, and the drop target for a drag
  * under the still: **Executed** (mono, 2 lines, green / red) and the action probabilities.
* **Tile 2 (y 648-1296)**:
  * **What TOD sees**: the full request-1 table, two columns, column-major, 7 rows each, p per row; rows
    changed since the last tick flash amber, then keep a softer highlight. The tick number is in the
    heading.
  * **Papers** line (2 lines)
  * **Situation sentence** (4 lines)
  * **Rule today** (2 lines)
* **Tile 3 (y 1296-1944)**:
  * **TOD's pick · top candidates**: top 3 with tweened bars, p and 2-line descriptions, the chosen one
    highlighted; tick + action probabilities in the heading
  * drop target
  * **Notes**: input-convention / cycle-break / excluded / guard, up to 3 lines
  * **Last 8 actions** chip strip
  * **Ground truth (not shown to TOD)**: 2 lines
  * **Latency**: 2 lines

Checked over all 254 ticks of the reference run (renders with the boundary bands pixel-checked): the lowest
content ends at y 612 / 1216 / 1889 in tiles 1 / 2 / 3 (boundaries 648 / 1296 / 1944). In the 3-px bands either
side of each separator, no tick has a drawn pixel.

**Single-flow layouts (`--height 1280` / `--height 2160`).** Same sections in one flow, top to bottom:

1. **Header**: title, run, tick.
2. **"what TOD was shown · request 2"**: the SoM frame with the marker. It is 729x409 at 1280 high and
   1144x642 at 2160 high.
3. **Verdict**: the 3 bars. They sit beside the frame at 1280 high and form a horizontal bar under it at 2160.
4. **Last 8 actions**: a list beside the frame at 1280 high, a chip strip at 2160.
5. **What TOD sees** + **papers**.
6. **Situation** + **today's rule**.
7. **Pick**:
   * action probabilities
   * top 3 candidates with descriptions
   * drop target
   * **executed**
   * notes
8. **Footer**: ground truth, latency.

**Transitions.** Each tick is held (time-lapse feel); the change to the next one is animated:
crossfade of the frame (250 ms), the action ring grows in (120-400 ms), changed facts slide up and glow
amber then settle to a softer highlight, a new pick slides in from the right and fades up, the confidence
bar tweens from the previous value, the action strip scrolls one chip, and a new verdict lands as a stamp
(scale 1.9 -> 1, 100-300 ms). All motion is over by ~0.45 s (`ANIM_END`), after which the window does no
work until the next tick. `VideoPanel.draw(cur, prev, t)` is a pure function of the two ticks and the time
since the new one arrived; the same code drives follow mode and the export.

Follow mode redraws on Tk's event loop at `--anim-fps` (default 60) during a transition only, using wall
time, so a slow frame is dropped rather than slowing the motion. Static parts (header, rule, card
backgrounds, ground truth, pick card, action strip) are drawn once per tick and cached; the settled frame
is cached too, so the window is idle between ticks.

**No live SoM overlay on the game.** An always-on-top click-through window drawing TOD's boxes over the
game was tried and is not shipped. The loop grabs the screen region (dxcam/mss), so an overlay would end up
in TOD's own input. The fix would be to grab the game window itself with `PrintWindow(PW_RENDERFULLCONTENT)`.
On this Unity build that comes back almost black: mean 7.5 vs 33.0, MSE about 3300 against the region grab,
0 % identical pixels. It is also ~12x slower (median 73 ms vs 6 ms). The grab path stays as it is, and the
numbered boxes appear only in the panel's SoM frame.

**Export (`--export-video`).** Composite time-lapse: the full raw game frame `raw_NNNN.png` (2280x1280,
crossfaded the same way) on the left at its on-screen size, the panel on the right. With the default
1944-high column, the frame is 3456x1944: the game is top-left and the area below it is black, as on the display, H.264 via ffmpeg (`libx264`, crf 20,
yuv420p, faststart). `--hold` = seconds per tick (default 0.5; frame counts use cumulative rounding so
the total is exactly ticks x hold), `--video-fps` (30), `--scale` (0.5 for a 1728x640 file),
`--panel-only` (panel alone). Reference run `runs/20261004_115735` (254 ticks, Day 3, Jorji Costava at
ticks 129-147):

```
python tools/viewer.py --run runs/20261004_115735 --layout video --export-video runs/20261004_115735/composite_video.mp4
python tools/viewer.py --run runs/20261004_115735 --layout video --export-video runs/20261004_115735/composite_jorji.mp4 --start 120 --end 150 --hold 0.48387
```

(H.264 yuv420p, 30 fps. With the 1944-high column: Jorji clip 3456x1944, 450 frames, 15.0 s, 6.0 MB. The full run
was last rendered with the 1280-high panel: 3456x1280, 3810 frames, 127.0 s, 41.5 MB, ~6.3 min to render.)
