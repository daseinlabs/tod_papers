"""Start / stop / export a Cap studio recording with no clicks.

  python tools/cap_record.py start [--window "TOD stage" | --display-name NAME] [--no-system-audio]
  python tools/cap_record.py stop  [--timeout 120]
  python tools/cap_record.py export [BUNDLE] [-o OUT.mp4] [--fps 30] [--resolution 3840x2160]
  python tools/cap_record.py status
  python tools/cap_record.py close-editor

start/stop use the desktop app's deep links (apps/desktop/src-tauri/src/deeplink_actions.rs):
  cap-desktop://action?value={"start_recording":{"capture_mode":{"window":"TOD stage"},"camera":null,
                                                 "mic_label":null,"capture_system_audio":true,"mode":"studio"}}
  (CaptureMode::Window(name): Cap picks the first window from list_windows() whose title == name;
   {"screen": NAME} with --display-name records a whole display instead.)
  Note: Cap 0.6 on Windows records a window as the DISPLAY cropped to the window's rect
  (crates/recording/src/capture_pipeline.rs), so the window must be on screen and uncovered -- tools/stage.py
  keeps itself on top, click-through.
  cap-desktop://action?value="stop_recording"
The URL is handed to Cap.exe as argv; tauri-plugin-single-instance forwards it to the running instance
(lib.rs handle_single_instance -> deeplink_actions::handle). The desktop path is used (rather than
`cap-cli record start`) because it applies the store settings written by tools/cap_setup.py, notably
studioRecordingQuality=ultra; the standalone CLI always uses RecordingDefaults::default() (balanced on >=16 GB RAM,
crates/recording/src/defaults.rs) and has no quality flag.

export uses the bundled CLI (cap-cli.exe export, apps/cli/src/export.rs), which renders project-config.json
(zoomSegments, cursor, background) exactly like the editor's export: MP4, Maximum compression.

State (bundle path of the take in progress) is kept in runs/cap_take.json.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import glob
import json
import os
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cap_setup  # noqa: E402

REC_DIR = os.path.join(cap_setup.CAP_DIR, "recordings")
STATE = os.path.join(os.path.dirname(HERE), "runs", "cap_take.json")


# ------------------------------------------------------------------------------------------------------------ helpers
def deep_link(action) -> None:
    url = "cap-desktop://action?" + urllib.parse.urlencode({"value": json.dumps(action, separators=(",", ":"))})
    cap_setup.launch_cap()
    # Second Cap.exe invocation exits immediately after forwarding argv to the running instance.
    subprocess.run([cap_setup.CAP_EXE, url], timeout=30, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def bundles() -> dict[str, float]:
    return {p: os.path.getmtime(p) for p in glob.glob(os.path.join(REC_DIR, "*.cap"))}


def load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(d: dict) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=2)


def read_meta(bundle: str) -> dict:
    try:
        with open(os.path.join(bundle, "recording-meta.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def meta_done(meta: dict) -> bool:
    # In-progress studio bundles carry status {"status":"InProgress"}; finished ones have segments and status
    # absent or "Complete". Accept either "segments" present without InProgress.
    st = meta.get("status")
    if isinstance(st, dict):
        st = st.get("status")
    return bool(meta.get("segments")) and st not in ("InProgress", "inProgress", "NeedsRemux")


user32 = ctypes.WinDLL("user32", use_last_error=True)
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def top_windows() -> list[tuple[int, str]]:
    out = []

    def cb(h, _):
        n = user32.GetWindowTextLengthW(h)
        if n and user32.IsWindowVisible(h):
            b = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, b, n + 1)
            out.append((h, b.value))
        return True
    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def close_editor() -> int:
    n = 0
    for h, title in top_windows():
        if title.startswith("Cap Editor"):
            user32.PostMessageW(h, 0x0010, 0, 0)  # WM_CLOSE
            n += 1
    return n


# ----------------------------------------------------------------------------------------------------------- commands
def cmd_start(a) -> int:
    st = load_state()
    if st.get("bundle") and not st.get("stopped"):
        sys.exit(f"a take is already in progress: {st['bundle']} (run `stop` first)")
    if a.display_name:
        target = cap_setup.resolve_display(a.display_name)["name"]
        mode = {"screen": target}
    else:
        win = cap_setup.resolve_window(a.window)
        if not win:
            sys.exit(f"no window titled {a.window!r} in `cap-cli targets windows` (start tools/stage.py first)")
        target = win["name"]
        mode = {"window": target}
        print(f"target window {target!r} id={win['id']} bounds={win.get('bounds')}")
    before = bundles()
    t_req = time.time()
    deep_link({"start_recording": {"capture_mode": mode, "camera": None, "mic_label": None,
                                   "capture_system_audio": not a.no_system_audio, "mode": "studio"}})
    bundle = None
    while time.time() - t_req < a.timeout:
        new = [p for p in bundles() if p not in before]
        if new:
            bundle = max(new, key=os.path.getmtime)
            break
        time.sleep(0.25)
    if not bundle:
        sys.exit("Cap did not create a recording bundle (is Cap running and past onboarding?)")
    save_state({"bundle": bundle, "requested": t_req, "target": mode, "stopped": False})
    print(f"recording -> {bundle}")
    return 0


def cmd_stop(a) -> int:
    st = load_state()
    bundle = st.get("bundle")
    if not bundle or st.get("stopped"):
        sys.exit("no take in progress (runs/cap_take.json)")
    deep_link("stop_recording")
    t = time.time()
    while time.time() - t < a.timeout:
        s = read_meta(bundle).get("status")
        if isinstance(s, dict) and s.get("status") == "Failed":
            sys.exit(f"Cap reports the recording failed: {s.get('error')}")
        if meta_done(read_meta(bundle)) and os.path.isfile(os.path.join(bundle, "project-config.json")):
            break
        time.sleep(0.5)
    else:
        sys.exit(f"bundle not finalised after {a.timeout}s: {bundle}")
    time.sleep(1.0)
    closed = close_editor()
    normalise_project(bundle)
    st["stopped"] = True
    save_state(st)
    print(f"stopped: {bundle}" + (f" (closed {closed} editor window)" if closed else ""))
    return 0


def normalise_project(bundle: str) -> None:
    """Cursor track hidden: the stage's game mirror (WGC, cursor included) shows the real cursor on the game
    exactly where/when TOD clicks; Cap's track would draw it at its screen position, which is not where the
    mirrored game tile is. (hide=False to get Cap's cursor back for a display recording.)"""
    p = os.path.join(bundle, "project-config.json")
    with open(p, encoding="utf-8") as fh:
        cfg = json.load(fh)
    cur = cfg.setdefault("cursor", {})
    cur.update({"hide": True, "hideWhenIdle": False, "size": 100, "raw": True, "motionBlur": 0.0})
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


def cmd_export(a) -> int:
    bundle = a.bundle or load_state().get("bundle")
    if not bundle:
        sys.exit("no bundle given and none in runs/cap_take.json")
    out = a.output or os.path.join(os.path.dirname(HERE), "runs", os.path.basename(bundle)[:-4] + ".mp4")
    close_editor()   # editor would otherwise be holding / rewriting project-config.json
    cmd = [cap_setup.CAP_CLI, "export", bundle, "-o", out, "--fps", str(a.fps), "--resolution", a.resolution,
           "--quality", "maximum", "--completion-json"]
    print(" ".join(f'"{c}"' if " " in c else c for c in cmd))
    r = subprocess.run(cmd)
    if r.returncode == 0:
        print(f"exported -> {out}")
    return r.returncode


def cmd_status(a) -> int:
    st = load_state()
    print(json.dumps(st, indent=2) if st else "no take state")
    print("Cap.exe running:", cap_setup.cap_running())
    print("windows:", [t for _, t in top_windows() if t.startswith("Cap")])
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Cap start/stop/export via deep links + cap-cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--window", default=cap_setup.DEFAULT_WINDOW, help='window title (default "TOD stage")')
    s.add_argument("--display-name", default=None, help="record this whole display instead of the window")
    s.add_argument("--no-system-audio", action="store_true")
    s.add_argument("--timeout", type=float, default=30.0)
    s = sub.add_parser("stop")
    s.add_argument("--timeout", type=float, default=180.0)
    s = sub.add_parser("export")
    s.add_argument("bundle", nargs="?")
    s.add_argument("-o", "--output")
    s.add_argument("--fps", type=int, default=30)
    s.add_argument("--resolution", default="3840x2160",
                   help="output WxH; default 4K UHD (the 3456x1944 stage window is scaled up)")
    sub.add_parser("status")
    sub.add_parser("close-editor")
    a = ap.parse_args(argv)
    if a.cmd == "close-editor":
        print("closed", close_editor())
        return 0
    return {"start": cmd_start, "stop": cmd_stop, "export": cmd_export, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
