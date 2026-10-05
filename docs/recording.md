# Recording the demo (OBS Studio)

One continuous take, composed and recorded live by OBS. The user can keep working on the PC: every capture is
occlusion-independent, and the audio comes from the game only. (The old Cap pipeline is in
`docs/recording_cap_legacy.md`. Cap display-crops, so anything covering the game ended up in the video.)

```
.venv-loop\Scripts\python.exe tools\produce_demo.py --days 1      # the Day 1 take
.venv-loop\Scripts\python.exe tools\produce_demo.py --rehearse    # same chain, no TOD credits (replay)
```

## Pieces

| tool | role |
|---|---|
| `tools/obs_setup.py` | idempotent: installs OBS ≥ 30.2 (winget) + the Move transition plugin (exeldro 3.2.1, GitHub "programdata" zip → `%ProgramData%\obs-studio\plugins`), writes the websocket config (port 4455, password in `.obs_ws_password`, gitignored), `user.ini`/`global.ini`, the `TOD` profile and the `TOD` scene collection, launches OBS in the tray and verifies every transform over the websocket. `--check` verifies only, `--rewrite` closes OBS and rewrites everything. |
| `tools/viewer.py --follow --layout video --obs` | the TOD sidebar: a normal top-level window titled `TOD viewer`, client 1176×1944 (three 648-px tiles). |
| `tools/tally.py --follow` | writes `runs/<ts>/tally/tally_current.png` atomically (1×1 transparent while the tally is down). |
| `tools/obs_director.py` | tails the run, switches scenes (rules below), Start/StopRecord, `runs/<ts>/scene_log.json`. |
| `tools/produce_demo.py` | the chain: tunnel health → obs_setup → viewer → tally → game (Steam, `--place 0,0`) → director → `demo_run.py --no-launch` → final tally hold → stop → verify → `report.md`. |

## OBS profile `TOD`

Canvas and output are 3840×2160 at 30 fps. Advanced output, NVENC H.264 (`obs_nvenc_h264_tex`): CQP 18, p5, hq,
keyint 2 s. Hybrid MP4 (crash-safe, no remux) to `runs/obs/`, then moved to `runs/<ts>/demo_dayN.mp4`. AAC
192 kbps, track 1. The collection has no Desktop Audio or Mic inputs (obs_setup removes them if they appear).
About 0.4 GB/min.

## Sources (one instance each, shared by every scene)

| name | kind | settings |
|---|---|---|
| `bg` | color source | 3840×2160 #101014 |
| `game` | Game Capture | window `PapersPlease:UnityWndClass:PapersPlease.exe`, match by exe, cursor on |
| `game_wgc` | Window Capture, WGC | same window, client area; **disabled** unless Game Capture is black (hybrid-GPU mismatch). The director/produce_demo switch it on automatically |
| `sidebar` | Window Capture, WGC | `TOD viewer:TkTopLevel:python.exe`, match by title, client area, no cursor |
| `tally` | Image | `runs/<ts>/tally/tally_current.png` (the director retargets it per run; OBS reloads on mtime) |
| `game_audio` | Application Audio Capture | PapersPlease.exe only |

## Scenes

Every item uses `pos` = top-left, alignment top-left, bounds = "scale to inner" with the box below. Sources that
are not in view are parked off-canvas (not hidden), so the Move transition (700 ms, cubic ease-in-out) can slide
and zoom them.

| scene | game (2280×1280) | sidebar (1176×1944) | tally (1920×1080) |
|---|---|---|---|
| GAME | (0,2) 3840×2156, ×1.68421 | (3840,22) 1280×2116, off right | (0,2160) off bottom |
| TWOUP | (0,361) 2560×1437, ×1.12281 | (2560,22) 1280×2116, ×1.08844 | off bottom |
| TOD1 | (−3840,361) 2560×1437, off left | (0,22) 3840×6348, ×3.26531 → tile 1 | off bottom |
| TOD2 | off left | (0,−2094) → tile 2 | off bottom |
| TOD3 | off left | (0,−4210) → tile 3 | off bottom |
| TALLY | as TWOUP | as TWOUP | (0,0) 3840×2160, ×2.0 |

## Director rules (the `cap_zoom_plan.py` cycle, live)

* Before the booth (title, menus, news, day intro): GAME.
* Booth entrant k (`gt.day_processed`): k mod 4 = 0, 1 → GAME; 2 → TWOUP; 3 → TOD1 for 20 s, then the pan to TOD2.
* A no-documents (Jorji) entrant, i.e. `no_documents_presented` on ≥ 3 consecutive ticks: TWOUP for 15 s, then
  TOD1 → TOD2.
* Min span 15 s: a switch waits until the current scene has been up 15 s and the latest wish wins, so short
  entrants fold into the previous view. Day-end and tally cues are exempt.
* Day end (gt NightScreen, or day d→d+1; the same events tally.py fires on): TWOUP, then TALLY once the tally PNG
  is up (≤ 6 s). When the PNG goes blank (12 s), GAME and the next day.
* Final (`--final-day` night, summary.md, or `obs_director.py --stop`): TALLY, held 12 s, then StopRecord.
* Ticks already on disk when it attaches are history: they update state only. A websocket drop leads to a
  reconnect and the current scene is re-applied. Poll errors are logged and never fatal.
* Watchdog every 5 s: a minimized game/viewer window is restored without activation, at the bottom of the
  z-order. The viewer is kept fully on-screen, because Tk does not paint off-screen parts and WGC would record
  them white.
* Pre-take checks: transforms, game source non-black (with the WGC fallback), sidebar non-black, disk space, NVENC
  in the OBS log.

## What the user must not do during a take

Don't minimize the game or the `TOD viewer` window (the watchdog restores them, but frames freeze until then).
Don't close them, and don't type into the game. Covering, moving or switching away from them is fine.

## GPU preference (optional, one-time)

Game Capture needs OBS and the game on the same adapter. On this hybrid laptop OBS runs on the RTX 4070 and the
hook worked without any change. If it ever records black, the take falls back to `game_wgc` automatically. To pin
both apps to the dGPU, run `tools/obs_setup.py --gpu-prefs`. It writes
`HKCU\Software\Microsoft\DirectX\UserGpuPreferences` `"<exe path>" = "GpuPreference=2;"` for `obs64.exe` and
`PapersPlease.exe`, the same value that Settings → System → Display → Graphics → "High performance" sets. Restart
both apps afterwards.

## Verification (`produce_demo.py --verify runs/<ts>`)

ffprobe (3840×2160, 30 fps, audio, duration ≈ recording), volumedetect (game audio not silent), and one ≤ 640 px
frame per scene type at its longest span in `scene_log.json` → `runs/<ts>/verify/`, `verification.json`, plus a
Recording section in `report.md`.
