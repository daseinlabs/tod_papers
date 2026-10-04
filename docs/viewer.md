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
q/Esc quit. `--width/--height` (default 1160x1980 physical px), `--x/--y` (default: 4 px right of the
window whose title contains "papers", top aligned; else x=2290, y=0).

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
