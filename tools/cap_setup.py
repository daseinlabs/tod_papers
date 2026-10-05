"""Configure Cap (cap.so desktop, Windows) for the demo take without touching its UI.

Cap keeps its settings in one tauri-plugin-store JSON file, %APPDATA%\\so.cap.desktop\\store (no extension):
  general_settings    camelCase GeneralSettingsStore   (apps/desktop/src-tauri/src/general_settings.rs)
  recording_settings  camelCase RecordingSettingsStore (apps/desktop/src-tauri/src/recording_settings.rs)
  automations         AutomationsStore {version, rules} (crates/automation/src/types.rs)

The store is loaded into memory on first access (`app.store("store")`) and written back by the app on change/exit;
there is no file watcher. So edits only apply if Cap is NOT running while we write, and a running Cap would
overwrite them. This script therefore: force-kills Cap.exe (no graceful exit, so it cannot save over us), backs the
store up, edits it, and relaunches Cap.exe (unless --no-launch).

What it sets (and why):
  recording_settings.mode = "studio"                 studio bundle (separate display/cursor tracks, editable zooms)
  recording_settings.target = window <id>            the "TOD stage" window (tools/stage.py) if it is open, by title
                                                       via `cap-cli targets windows --json`; store form
                                                       {"variant": "window", "id": "<hwnd>"}. Not open: the display
                                                       (--display-name). The take itself is targeted by name in
                                                       cap_record start's deep link (capture_mode {"window": NAME}).
  recording_settings.systemAudio = true, micName = null, cameraId = null
  general_settings.studioRecordingQuality = "ultra"  StudioQuality::Ultra (crates/recording/src/lib.rs)
  general_settings.autoZoomOnClicks = false          our zooms come from tools/cap_zoom_plan.py
  general_settings.recordingCountdown = null         start immediately (deterministic t=0)
  general_settings.custom_cursor_capture2 = true     cursor recorded as a separate track (editable/smoothable)
  general_settings.maxFps = --fps (default 60)
  general_settings.postStudioRecordingBehaviour = "openEditor" kept, but an automation rule
      {trigger: studioRecordingFinished, actions: [skipEditor]} suppresses the editor after stop
      (apps/desktop/src-tauri/src/automation.rs studio_recording_editor_behaviour)
  general_settings.enableNotifications = false, autoCreateShareableLink = false
  hideDockIcon is macOS-only and left alone.

Cursor size/smoothing are per-project (project-config.json "cursor"), not store settings; tools/cap_record.py stop
normalises them in the new bundle.

Cap's cursor track is hidden per take by cap_record stop: the stage's game mirror already draws the real cursor.

Usage:  python tools/cap_setup.py [--window "TOD stage"] [--fps 60] [--no-launch] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

APPDATA = os.environ.get("APPDATA", "")
CAP_DIR = os.path.join(APPDATA, "so.cap.desktop")
STORE = os.path.join(CAP_DIR, "store")
LOCAL = os.environ.get("LOCALAPPDATA", "")
CAP_EXE = os.path.join(LOCAL, "Cap", "Cap.exe")
CAP_CLI = os.path.join(LOCAL, "Cap", "cap-cli.exe")
DEFAULT_DISPLAY = "Dell XPS 15 SDC414D Display"
DEFAULT_WINDOW = "TOD stage"
SKIP_RULE_ID = "tod-papers-skip-editor"


def cap_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Cap.exe", "/NH"], capture_output=True, text=True).stdout
    return "Cap.exe" in out


def kill_cap(timeout: float = 15.0) -> None:
    if not cap_running():
        return
    subprocess.run(["taskkill", "/F", "/T", "/IM", "Cap.exe"], capture_output=True)
    t = time.time()
    while cap_running() and time.time() - t < timeout:
        time.sleep(0.5)
    if cap_running():
        sys.exit("Cap.exe did not exit")


def launch_cap(wait: float = 30.0) -> None:
    if cap_running():
        return
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    # Cap hides its own windows (recording controls etc.) from capture with WDA_EXCLUDEFROMCAPTURE -- unless it
    # thinks the desktop is streamed, which it concludes from the installed "Virtual Display Driver" adapter
    # (apps/desktop/src-tauri/src/platform/win.rs capture_streamed_display_reason); then its control bar is
    # recorded (test take 20261004 16:19). CAP_WINDOW_CAPTURE_EXCLUSION=on is Cap's own override (env of this
    # Cap.exe process only).
    env = dict(os.environ, CAP_WINDOW_CAPTURE_EXCLUSION="on")
    subprocess.Popen([CAP_EXE], creationflags=flags, close_fds=True, env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t = time.time()
    while not cap_running() and time.time() - t < wait:
        time.sleep(0.5)
    if not cap_running():
        sys.exit("Cap.exe did not start")
    time.sleep(4.0)  # let the store, deep-link handler and recording actor come up


def list_screens() -> list[dict]:
    r = subprocess.run([CAP_CLI, "targets", "screens", "--json"], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        sys.exit(f"cap-cli targets screens failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


def resolve_display(name: str) -> dict:
    screens = list_screens()
    for s in screens:
        if s.get("name") == name:
            return s
    sys.exit(f"no screen named {name!r}; have: {[s.get('name') for s in screens]}")


def list_windows() -> list[dict]:
    r = subprocess.run([CAP_CLI, "targets", "windows", "--json"], capture_output=True, timeout=30)
    if r.returncode != 0:
        sys.exit(f"cap-cli targets windows failed: {r.stderr.decode(errors='replace').strip()}")
    return json.loads(r.stdout.decode("utf-8-sig"))


def resolve_window(title: str) -> dict | None:
    """Cap's window target with exactly this title (the deep link's {"window": NAME} matches the same way)."""
    return next((w for w in list_windows() if w.get("name") == title), None)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--window", default=DEFAULT_WINDOW, help='window title to target (default "TOD stage")')
    ap.add_argument("--display-name", default=DEFAULT_DISPLAY, help="store target when the window is not open")
    ap.add_argument("--fps", type=int, default=60, help="maxFps for studio recording (default 60)")
    ap.add_argument("--no-launch", action="store_true", help="leave Cap closed after editing")
    ap.add_argument("--dry-run", action="store_true", help="print the changes, do not kill Cap or write")
    a = ap.parse_args(argv)

    if not os.path.isfile(STORE):
        sys.exit(f"Cap store not found: {STORE} (launch Cap once and finish onboarding)")
    win = resolve_window(a.window) if a.window else None
    if win:
        target = {"variant": "window", "id": str(win["id"])}
        print(f"target window: {win['name']!r} id={win['id']} bounds={win.get('bounds')}")
    else:
        disp = resolve_display(a.display_name)
        target = {"variant": "display", "id": str(disp["id"])}
        print(f"window {a.window!r} not open: store target = display {disp['name']} id={disp['id']} "
              "(cap_record start targets the window by name anyway)")

    if not a.dry_run:
        kill_cap()
    with open(STORE, encoding="utf-8") as fh:
        store = json.load(fh)

    gs = store.setdefault("general_settings", {})
    rs = store.setdefault("recording_settings", {})
    want_gs = {
        "studioRecordingQuality": "ultra",
        "autoZoomOnClicks": False,
        "recordingCountdown": None,
        "custom_cursor_capture2": True,
        "maxFps": a.fps,
        "postStudioRecordingBehaviour": "openEditor",
        "mainWindowRecordingStartBehaviour": "close",
        "enableNotifications": False,
        "autoCreateShareableLink": False,
        "disableAutoOpenLinks": True,
    }
    want_rs = {
        "mode": "studio",
        "target": target,
        "systemAudio": True,
        "micName": None,
        "cameraId": None,
    }
    changes = []
    for d, want, label in ((gs, want_gs, "general_settings"), (rs, want_rs, "recording_settings")):
        for k, v in want.items():
            if d.get(k, "<unset>") != v:
                changes.append(f"{label}.{k}: {json.dumps(d.get(k, '<unset>'))} -> {json.dumps(v)}")
                d[k] = v

    autom = store.get("automations") or {"version": 1, "rules": []}
    autom.setdefault("version", 1)
    rules = [r for r in autom.get("rules", []) if r.get("id") != SKIP_RULE_ID]
    rules.append({"id": SKIP_RULE_ID, "name": "tod_papers: no editor after studio recording", "enabled": True,
                  "trigger": "studioRecordingFinished", "matchMode": "all", "conditions": [],
                  "actions": [{"type": "skipEditor"}]})
    if autom.get("rules") != rules:
        changes.append("automations: rule skipEditor on studioRecordingFinished")
    autom["rules"] = rules
    store["automations"] = autom

    for c in changes:
        print("  " + c)
    if not changes:
        print("  store already configured")
    if a.dry_run:
        return 0
    if changes:
        bak = f"{STORE}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(STORE, bak)
        tmp = STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(store, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, STORE)
        print(f"wrote {STORE} (backup {bak})")
    if not a.no_launch:
        launch_cap()
        print("Cap.exe running")
    return 0


if __name__ == "__main__":
    sys.exit(main())
