> **Legacy (replaced by OBS, see docs/recording.md).** Cap display-crops a window, so anything covering the game is
> recorded, and its audio is system loopback. The cap_* / stage.py tools stay in the tree but are no longer in the
> produce_demo chain.

# Recording the demo video with Cap

One unbroken live take of `tools/demo_run.py` (cold start, Day 1 to the Day 3 night) with game audio. The recording
is the whole display: the game window plus the behind-the-scenes viewer. Afterwards a script turns the run's tick
log into Cap zoom segments. The video then alternates between zoomed IN on the game and OUT on game plus viewer.
The Day 3 Jorji "no documents / interrogate" entrant and every end of day are always OUT.

Tested with Cap 0.6.0 for Windows (winget `CapSoftware.Cap`, installed in `%LOCALAPPDATA%\Cap\Cap.exe`). The schema
notes below come from the Cap source at github.com/CapSoftware/Cap (0.6.1 tree).

## 0. What Cap does (and the parts we rely on)

| need | Cap | source |
|---|---|---|
| whole display at native resolution | Studio mode, target = display. Records physical pixels (an earlier take logged `width=3456 height=2160`, h264_nvenc). | `crates/recording/src/sources/screen_capture` |
| system audio | "System audio" toggle (WASAPI loopback on Windows). Stored as `recording_settings.systemAudio`. | `crates/recording/tests/windows_loopback.rs` |
| zoom segments | Editor zoom track. Manual segments (fixed center) and auto (follows cursor clusters). `autoZoomOnClicks` creates auto segments when a recording finishes. | `crates/project/src/configuration.rs` (`ZoomSegment`, `ZoomMode`), `apps/desktop/src-tauri/src/recording.rs` |
| timeline storage | `<recording>.cap/project-config.json` → `timeline.zoomSegments`. Loaded with `ProjectConfiguration::load`, so a hand-edited file is read when the editor opens. | `configuration.rs` `load()` / `write()` |
| no frame / background | `background.padding/rounding/shadow` = 0, `crop` = `{position:{x,y}, size:{x,y}}` (u32 px, `Crop`). Padding 0 means the cropped display fills the output. | `configuration.rs` `BackgroundConfiguration` |
| cursor | Captured as data (`cursor.json`) and drawn by the editor. Settings: hide, size, "Smooth Movement" (`cursor.raw` = !smooth), motion blur, click ripple. | `configuration.rs` cursor config, `apps/desktop/src/routes/editor/ConfigSidebar.tsx` |
| export | MP4 (also GIF and MOV). Resolution 720p / 1080p / 4K, scaled to fit while keeping the source aspect. FPS 15 / 30 / 60. Compression Maximum / Social / Web / Potato. | `apps/desktop/src/routes/editor/ExportPage.tsx`, `Header.tsx`, `crates/export/src/mp4.rs`, `crates/rendering/src/lib.rs` `get_output_size` |

Zoom segment JSON (serde camelCase). `start` and `end` are timeline seconds:

```json
{"start": 0.0, "end": 189.5, "amount": 1.5158,
 "mode": {"manual": {"x": 0.0, "y": 0.0}},
 "glideDirection": "none", "glideSpeed": 0.5, "instantAnimation": false, "edgeSnapRatio": 0.25}
```

`mode` is either `"auto"` or `{"manual": {x, y}}`. For a manual segment, `x, y` is the zoom anchor c in [0,1]. The
visible part of the display is `[c(a-1)/a, (c(a-1)+1)/a]` (`crates/rendering/src/zoom.rs`). If the game sits at the
top-left, the anchor is (0,0) and the amount is `display_w / game_w`.

Recordings are saved in `%APPDATA%\so.cap.desktop\recordings\<Display name> (Display) <date time>.cap\` (default;
Settings → recordings folder overrides it). Each bundle contains `project-config.json`, `recording-meta.json`,
`recording-logs.log`, `content\segments\segment-0\` (display video, audio tracks, `cursor.json`) and
`assets\`.

## 1. Screen layout

Use the laptop panel `\\.\DISPLAY1`. Cap calls it "Dell XPS 15 SDC414D Display". It is 3456 x 2160 physical pixels
at 250 % scaling. Windows reports it as 1382 x 864 to DPI-unaware tools. A second virtual display
(`\\.\DISPLAY5`, "VDD by MTT", 1920 x 1080 at 3456,2160) is also attached. Do not record that one, and keep both
windows off it.

```
x=0                         2280 2290                      3456
+----------------------------+--+---------------------------+  y=0
| Papers, Please             |  | TOD viewer (--follow)     |
| client 2280 x 1280         |  | sidebar, ~1160 px wide    |
+----------------------------+  |                           |  y~1280
| (desktop / free)           |  |                           |
+----------------------------+--+---------------------------+  y=2160 (taskbar ~120 px: auto-hide it)
```

* The game is 2280 x 1280 client (the loop expects this, see `tools/reset_game.py check_geometry`). `demo_run.py`
  relaunches the game and moves it on-screen. It also logs the client rect to `runs/demo_<ts>.json`, and the zoom
  plan can read that file directly.
* The viewer places itself 4 px to the right of the game window. With no game window it goes to x=2290. Its size and
  layout are the viewer's own business (`docs/viewer.md`).
* Hide the taskbar (Settings → Personalization → Taskbar → automatically hide), close notifications, and turn on
  Do Not Disturb. Leave nothing on top of either window.
* The display is 16:10. The zoom plan crops the video to the top 16:9 band, `background.crop` =
  `{"position":{"x":0,"y":0},"size":{"x":3456,"y":1944}}`, so the bottom 216 px (taskbar area) never appear.
  The sidebar sits at (2280,0), 1176 x 1944. The tally pops up centred over everything or over the sidebar. Zoomed IN
  is anchor (0,0) at amount 3456/2280 = 1.516, which frames exactly the 2280 x 1280 game. TALLY zooms to fit the tally
  rect inside the crop.
* Check the geometry once both windows are up: `python tools/window_rects.py --out runs/rects.json`. It prints the
  game client, viewer window and display rects in physical pixels, plus notes for overlap, off-display or minimized
  windows.

## 2. Cap settings: `tools/cap_setup.py`

Nothing is set by hand. `python tools/cap_setup.py` force-kills Cap, backs up and edits the settings store, and
relaunches Cap. Re-run it any time; it prints only what it changed.

* Store: `%APPDATA%\so.cap.desktop\store` (one JSON file, tauri-plugin-store). Cap loads it into memory on first use
  and writes it back itself; there is no file watcher (`apps/desktop/src-tauri/src/general_settings.rs`
  `GeneralSettingsStore::get` → `app.store("store")`). So the file is only edited while Cap is not running, and Cap
  is killed with `taskkill /F` so it cannot save its in-memory copy over ours. Backup: `store.bak-<ts>`.
* `recording_settings`: `mode` = `"studio"`, `target` = `{"variant":"display","id":<id>}` (id of
  "Dell XPS 15 SDC414D Display", 3456 x 2160, resolved via `cap-cli targets screens --json`; never the "Generic PnP
  Monitor" virtual display), `systemAudio` = true, `micName` = null, `cameraId` = null.
* `general_settings`: `studioRecordingQuality` = `"ultra"` (`StudioQuality`, `crates/recording/src/lib.rs`; Windows
  encoder bpp 1.0 instead of 0.3), `autoZoomOnClicks` = false, `recordingCountdown` = null (t=0 is the deep link),
  `maxFps` = 60 (`--fps`), `custom_cursor_capture2` = true (cursor as data, drawn at export),
  `enableNotifications` = false, `autoCreateShareableLink` = false.
* `automations`: rule `{trigger: "studioRecordingFinished", actions: [{"type":"skipEditor"}]}`. Cap consults it
  before opening the editor after a studio recording (`apps/desktop/src-tauri/src/automation.rs`
  `studio_recording_editor_behaviour`), so nothing pops up and nothing holds `project-config.json`.
* Cursor size/smoothing are per-project (`project-config.json` → `cursor`), not store settings.
  `cap_record.py stop` sets them on the new bundle: visible, size 100, `raw` = true (no smoothing, TOD's clicks land
  exactly where and when they happen), motion blur 0.
* `hideDockIcon` is macOS-only; left alone.

## 3. The take

From the repo root, with the loop venv:

```
python tools/cap_setup.py                 # once per session (restarts Cap)
python tools/viewer.py --follow --layout video   # own terminal; full-detail sidebar right of the game, follows the newest runs/<ts>
                                                 #   default 1176x1944 at (2280,0) = the three zoom tiles
# (no som_overlay step: the live box overlay is not shipped, see docs/viewer.md "No live SoM overlay")
python tools/tally.py --follow            # own terminal; writes runs/<ts>/tally_schedule.json
python tools/cap_record.py start          # deep link -> Cap studio recording of the Dell display, system audio
python tools/demo_run.py                  # cold start -> Day 3 night, stops on its own (docs/demo.md)
python tools/cap_record.py stop           # after the final tally has been up a few seconds
python tools/cap_zoom_plan.py runs/<ts> --rects runs/demo_<demo_ts>.json --cap-project <bundle> --record-start-from-cap
python tools/cap_record.py export         # -> runs/<bundle name>.mp4, 3840x2160 30 fps, Maximum
```

* `start` sends `cap-desktop://action?value={"start_recording":{"capture_mode":{"screen":"Dell XPS 15 SDC414D
  Display"},"camera":null,"mic_label":null,"capture_system_audio":true,"mode":"studio"}}` by running
  `Cap.exe <url>`; tauri-plugin-single-instance forwards it to the running Cap
  (`apps/desktop/src-tauri/src/deeplink_actions.rs`, `lib.rs` `handle_single_instance`). It waits for the new
  `<name>.cap` bundle in `%APPDATA%\so.cap.desktop\recordings` and stores its path in `runs/cap_take.json`.
* `stop` sends `cap-desktop://action?value="stop_recording"`, waits for `recording-meta.json` status `Complete`,
  closes any "Cap Editor" window (WM_CLOSE), and normalises the cursor config. `status` shows the take state.
* Why the desktop deep link and not `cap-cli record start`: the CLI records standalone with
  `RecordingDefaults::default()` (`crates/recording/src/defaults.rs`): Balanced quality on a >=16 GB machine, no
  quality flag. The deep link path uses the store, so Ultra applies. `cap-cli record start --screen <id>
  --system-audio --detach` / `cap-cli record stop` is the fallback if the app is unavailable.
* The run dir is printed by the loop (`[loop] logging to runs/<ts>`); the bundle path is in `runs/cap_take.json`.

## 4. Zoom plan → Cap

```
python tools/cap_zoom_plan.py runs/<ts> --rects runs/demo_<demo_ts>.json ^
    --cap-project "%APPDATA%\so.cap.desktop\recordings\<name>.cap" --record-start-from-cap
```

* Geometry: `--rects` takes `runs/rects.json` (window_rects) or `runs/demo_<ts>.json` (demo_run). You can also give
  `--game-rect x,y,w,h --display 3456x2160` explicitly.
* t=0: `--record-start-from-cap` reads the first admitted screen frame from the bundle's `recording-logs.log` (UTC).
  The alternative is `--record-start 2026-10-04T12:00:03` (local time). `--offset 0.5` shifts every boundary later
  if the cuts look early.
* Zoom levels, all inside the crop: **IN** = the game (manual anchor (0,0), amount 3456/2280 = 1.516).
  **TWOUP** = the full crop, game plus sidebar (no segment, amount 1). **TOD** = the sidebar column (x 2280-3456)
  at amount 3456/1176 = 2.94, which shows one 1176 x 661 band. The sidebar is three stacked 16:9 tiles: tile1
  y 0-648, tile2 648-1296, tile3 1296-1944. These are the defaults. Override them with
  `--tiles "x,y,w,h;x,y,w,h;..."` or a `tiles` key in the `--rects` JSON.
* A TOD beat is two contiguous manual segments: tile1 for the rest of the span (at least `--tod-t1 20` s), then
  tile2 for `--tod-t2 12` s. Both use the same amount with different anchors. `glideDirection` is none and
  `instantAnimation` is false, so Cap pans from tile1 to tile2. A TOD span shorter than T1+T2 extends into the
  next span, unless the next span is a day end or a tally.
* Cycle, per day: `--in 2` entrants IN → `--twoup 1` TWOUP → `--tod 1` TOD → IN … (`--out` is an alias of
  `--twoup`; `--tod 0` disables TOD). Each day restarts with IN, and the intro (title, news) is IN. `--eod-lead 8`
  goes to TWOUP 8 s before each night screen. `--lead 0.3` starts each zoom change slightly early. Entrant spans
  shorter than `--min-span 15` s keep the previous zoom.
* Forced: the no-documents entrant (manual step N1..N5, a visible interrogate prompt, or
  `no_documents_presented=yes` on 3 or more consecutive ticks, `--nodocs-min-run`) is split. The first half is
  TWOUP and the second half is TOD; in practice this is Jorji on Day 3. The cycle restarts with IN after it.
  Each day's end is TWOUP.
* Tally beats: the script reads `runs/<ts>/tally_schedule.json` automatically, or `--tally-schedule <json>`. The file
  is a list of `{"start", "end", "reason"}`, with ISO or unix times; `end: null` means the window is still up at
  the end. Each tally window becomes a TALLY span: OUT by default, or a manual zoom on the tally window with
  `--tally-rect x,y,w,h`. A schedule entry can also carry its own `"rect": [x,y,w,h]`, and that wins. That rect can also come from `--rects` (`tally_in_display`; run `window_rects.py` while
  the tally is showing). If a tally would follow an IN span directly, the IN span is cut `--eod-lead` s early.
  A day end therefore always runs IN/TOD → TWOUP → TALLY → next day IN.
* Writes: copies `project-config.json` to `project-config.json.bak-<ts>`, replaces `timeline.zoomSegments` (use
  `--merge` to keep segments that do not overlap ours), and sets padding, rounding and shadow to 0 and the crop to `--crop` (default `auto` = top 16:9 band; `none` or `x,y,w,h`)
  (`--keep-style` skips that). Times go through `timeline.segments`, so trimming the head or tail in Cap first is
  fine. If you trim later, re-run the script.
* Prints a table (video time ranges, IN/OUT, reason). `--json plan.json` also saves it.

Example on the reference run `runs/20261004_115735` (Day 3 only, t=0 set 15 s before tick 0, game at 0,0, synthetic
final tally from the night screen to the end, video length 1580 s):

```
video start  video end     dur  zoom  reason
   00:00.00   03:10.08  190.1s  IN    intro (title/load); day 3 intro (news); day 3 entrants #1-2
   03:10.08   09:13.09  363.0s  TWOUP day 3 entrant #3
   09:13.09   10:38.02   84.9s  TOD   day 3 entrant #4 [tile1]
   10:38.02   10:50.02   12.0s  TOD   day 3 entrant #4 [tile2]
   10:50.02   14:07.24  197.2s  IN    day 3 entrants #5-6
   14:07.24   15:25.19   77.9s  TWOUP day 3 entrant #7: NO DOCUMENTS / interrogate (forced)
   15:25.19   16:31.14   65.9s  TOD   day 3 entrant #7: NO DOCUMENTS / interrogate (forced) [tile1]
   16:31.14   16:43.14   12.0s  TOD   day 3 entrant #7: NO DOCUMENTS / interrogate (forced) [tile2]
   16:43.14   21:46.88  303.7s  IN    day 3 entrants #8-9
   21:46.88   25:16.70  209.8s  TWOUP day 3 entrant #10
   25:16.70   25:24.97    8.3s  TOD   day 3 entrant #11 [tile1]
   25:24.97   25:33.24    8.3s  TOD   day 3 entrant #11 [tile2]
   25:33.24   25:42.00    8.8s  TWOUP day 3 end-of-day
   25:42.00   26:20.00   38.0s  TALLY tally: day 3 final tally
```

## 5. Export: `tools/cap_record.py export`

`cap_record.py export [BUNDLE] [-o OUT.mp4] [--fps 30] [--resolution 3840x2160]` runs the CLI that ships with the
app, `%LOCALAPPDATA%\Cap\cap-cli.exe export <bundle> -o <out> --fps 30 --resolution 3840x2160 --quality maximum`
(`apps/cli/src/export.rs`). It renders `project-config.json` (zoom segments, crop, cursor) with the same renderer as
the editor. The zoom plan crops to the top 16:9 band (3456 x 1944), so 4K UHD is 3840 x 2160. Purely local; nothing
is uploaded. Opening the editor is optional (to scrub boundaries): `cap-desktop://action?value={"open_editor":
{"project_path":"<bundle>"}}`; run `cap_record.py close-editor` before re-running the zoom plan or exporting.

Verified 2026-10-04 (15 s test take of the Dell display with `tada.wav` played twice, synthetic zoom plan
`--game-rect 0,0,2280,1280 --display 3456x2160`, two IN segments at amount 1.516):

```
segment-0/display.mp4       h264 3456x2160, 16.0 s, ~38 fps avg (VFR, maxFps 60), 6.2 Mb/s
segment-0/system_audio.m4a  aac 48000 Hz stereo, RMS -9 dB per second (system audio captured)
recording-meta.json         status Complete; segments: display, system_audio, cursor, keyboard (no camera, no mic)
export.mp4                  h264 Main 3840x2160 yuv420p 30/1 fps (481 frames, 16.03 s), 15.4 Mb/s; aac 48000 Hz stereo
```
No editor window opened at stop; Cap's only window afterwards was the main "Cap" window.
