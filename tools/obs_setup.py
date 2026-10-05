r"""obs_setup.py -- idempotent OBS Studio setup for the one-take demo recording (docs/recording.md).

    .venv-loop\Scripts\python.exe tools\obs_setup.py              # install/configure if needed, launch, verify
    .venv-loop\Scripts\python.exe tools\obs_setup.py --rewrite    # close OBS, rewrite every config file, relaunch
    .venv-loop\Scripts\python.exe tools\obs_setup.py --check      # verify a running OBS only (no writes)
    .venv-loop\Scripts\python.exe tools\obs_setup.py --gpu-prefs  # opt-in: Windows Graphics "High performance"

Steps (each skipped when already done):
  1. OBS Studio >= 30.2 (`winget install OBSProject.OBSStudio`); Move transition plugin (exeldro, the
     "windows-programdata" zip from GitHub releases, unpacked to %ProgramData%\obs-studio\plugins -- no admin).
  2. With OBS closed: obs-websocket (plugin_config\obs-websocket\config.json: port 4455, auth on, password from
     .obs_ws_password, generated once, gitignored), global.ini/user.ini (no first-run wizard, profile + collection
     TOD, no exit/record prompts), profile basic.ini + recordEncoder.json (3840x2160 30 fps, Advanced output,
     NVENC H.264 CQP 18 p5 keyint 2 s, Hybrid MP4, runs\obs\), scene collection TOD.json (SCENES below).
  3. Launch `obs64.exe --collection TOD --profile TOD --minimize-to-tray --disable-updater
     --disable-shutdown-check` (cwd bin\64bit) and wait for the websocket.
  4. Verify over the websocket: video settings, every scene item transform (re-set if off), transition Move 700 ms,
     Desktop Audio / Mic inputs absent (removed if present), record directory.
--gpu-prefs writes HKCU\Software\Microsoft\DirectX\UserGpuPreferences  "<exe path>" = "GpuPreference=2;"
(the same value Settings > System > Display > Graphics > "High performance" writes) for obs64.exe and
PapersPlease.exe. It is opt-in; without it Game Capture can be black on this hybrid laptop, and the take falls back to
the Window Capture (WGC) twin of the game source automatically (produce_demo / obs_director --check).
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
RUNS_OBS = os.path.join(ROOT, "runs", "obs")
PW_FILE = os.path.join(ROOT, ".obs_ws_password")
OBS_DIR = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "obs-studio")
OBS_BIN = os.path.join(OBS_DIR, "bin", "64bit")
OBS_EXE = os.path.join(OBS_BIN, "obs64.exe")
CFG = os.path.join(os.environ["APPDATA"], "obs-studio")
PLUGINS_PD = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "obs-studio", "plugins")
MOVE_VER = "3.2.1"
MOVE_URL = (f"https://github.com/exeldro/obs-move-transition/releases/download/{MOVE_VER}/"
            f"move-transition-{MOVE_VER}-windows-programdata.zip")
HOST, PORT = "127.0.0.1", 4455
NAME = "TOD"   # profile and scene collection

CANVAS = (3840, 2160)
FPS = 30
CQP = 18
TRANSITION, TRANSITION_MS = "Move", 700
GAME_WINDOW = "PapersPlease:UnityWndClass:PapersPlease.exe"   # title:class:exe (':' in a title would be '#3A')
SIDEBAR_WINDOW = "TOD viewer:TkTopLevel:python.exe"           # tools/viewer.py --obs (matched by exact title)
BG_ABGR = 0xFF141010                                          # #101014

# ---- layout (research doc table). Every item: pos = top-left, bounds box, scale-to-inner (aspect kept), so the
# numbers hold even if a source's pixel size drifts. Native sizes: game 2280x1280, sidebar 1176x1944 (three 648 px
# tiles), tally 1920x1080.
SB_TILE = 2116                     # one 648-px sidebar tile at the TOD zoom (3840/1176 = 3.26531) -> 2116 px
GAME_FULL = (0, 2, 3840, 2156)     # scale 1.68421
GAME_TWO = (0, 361, 2560, 1437)    # scale 1.12281
GAME_OFF = (-3840, 361, 2560, 1437)
SB_TWO = (2560, 22, 1280, 2116)    # scale 1.08844
SB_OFF = (3840, 22, 1280, 2116)
TALLY_ON = (0, 0, 3840, 2160)      # scale 2.0
TALLY_OFF = (0, 2160, 3840, 2160)  # parked below the canvas: Move slides it up


def sb_tod(k: int) -> tuple:
    return (0, 22 - (k - 1) * SB_TILE, 3840, 6348)   # scale 3.26531 (1176x1944 -> 3840x6348)


SCENES: dict[str, dict[str, tuple]] = {
    "GAME":  {"game": GAME_FULL, "sidebar": SB_OFF, "tally": TALLY_OFF},
    "TWOUP": {"game": GAME_TWO, "sidebar": SB_TWO, "tally": TALLY_OFF},
    "TOD1":  {"game": GAME_OFF, "sidebar": sb_tod(1), "tally": TALLY_OFF},
    "TOD2":  {"game": GAME_OFF, "sidebar": sb_tod(2), "tally": TALLY_OFF},
    "TOD3":  {"game": GAME_OFF, "sidebar": sb_tod(3), "tally": TALLY_OFF},
    "TALLY": {"game": GAME_TWO, "sidebar": SB_TWO, "tally": TALLY_ON},
}
SCALE_FILTER = {"game": "bicubic", "game_wgc": "bicubic", "sidebar": "lanczos", "tally": "lanczos"}
# game_wgc = Window Capture (WGC) twin of `game`, same transform, hidden; enabled instead of `game` when Game Capture
# is black (hybrid-GPU adapter mismatch). Move matches items by source name, so either one animates.
TWIN = {"game": "game_wgc"}


def log(msg: str) -> None:
    print(f"[obs_setup {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- 1: install
def obs_version() -> tuple | None:
    if not os.path.exists(OBS_EXE):
        return None
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"(Get-Item '{OBS_EXE}').VersionInfo.ProductVersion"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        return tuple(int(x) for x in out.split(".")[:3])
    except Exception:
        return (0, 0, 0)


def ensure_obs() -> tuple:
    v = obs_version()
    if v and v >= (30, 2, 0):
        return v
    log(f"OBS {'missing' if not v else v}: winget install OBSProject.OBSStudio (a few minutes)")
    r = subprocess.run(["winget", "install", "--id", "OBSProject.OBSStudio", "-e", "--silent",
                        "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"],
                       capture_output=True, text=True, timeout=1500)
    v = obs_version()
    if not v or v < (30, 2, 0):
        raise SystemExit(f"OBS install failed (winget rc={r.returncode}): {(r.stdout or '')[-300:]}")
    return v


def move_dll() -> str | None:
    for p in (os.path.join(PLUGINS_PD, "move-transition", "bin", "64bit", "move-transition.dll"),
              os.path.join(OBS_DIR, "obs-plugins", "64bit", "move-transition.dll")):
        if os.path.exists(p):
            return p
    return None


def ensure_move() -> str:
    p = move_dll()
    if p:
        return p
    os.makedirs(RUNS_OBS, exist_ok=True)
    z = os.path.join(RUNS_OBS, f"move-transition-{MOVE_VER}-windows-programdata.zip")
    log(f"downloading the Move transition plugin {MOVE_VER} from GitHub releases")
    urllib.request.urlretrieve(MOVE_URL, z)
    os.makedirs(PLUGINS_PD, exist_ok=True)
    with zipfile.ZipFile(z) as zf:
        zf.extractall(PLUGINS_PD)
    p = move_dll()
    if not p:
        raise SystemExit("Move transition: zip unpacked but move-transition.dll not found")
    return p


# ---------------------------------------------------------------- 2: config files
def password() -> str:
    if os.path.exists(PW_FILE):
        with open(PW_FILE, encoding="utf-8") as fh:
            pw = fh.read().strip()
        if pw:
            return pw
    pw = secrets.token_urlsafe(18)
    with open(PW_FILE, "w", encoding="utf-8") as fh:
        fh.write(pw + "\n")
    log(f"generated the obs-websocket password -> {os.path.relpath(PW_FILE, ROOT)} (gitignored)")
    return pw


def _ini_update(path: str, values: dict[str, dict[str, str]]) -> None:
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    if os.path.exists(path):
        cp.read(path, encoding="utf-8-sig")
    for sec, kv in values.items():
        if not cp.has_section(sec):
            cp.add_section(sec)
        for k, v in kv.items():
            cp.set(sec, k, str(v))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        cp.write(fh, space_around_delimiters=False)


def write_app_config(ver: tuple, pw: str) -> None:
    last = (ver[0] << 24) | (ver[1] << 16) | ver[2]
    _ini_update(os.path.join(CFG, "global.ini"), {"General": {"LastVersion": last, "Pre31Migrated": "true"}})
    _ini_update(os.path.join(CFG, "user.ini"), {
        "General": {"FirstRun": "true", "EnableAutoUpdates": "false", "ConfirmOnExit": "false"},
        "Basic": {"Profile": NAME, "ProfileDir": NAME, "SceneCollection": NAME, "SceneCollectionFile": NAME},
        "BasicWindow": {"SysTrayEnabled": "true", "SysTrayWhenStarted": "true", "WarnBeforeStartingRecord": "false",
                        "WarnBeforeStoppingRecord": "false", "RecordWhenStreaming": "false"},
    })
    ws = os.path.join(CFG, "plugin_config", "obs-websocket")
    os.makedirs(ws, exist_ok=True)
    with open(os.path.join(ws, "config.json"), "w", encoding="utf-8") as fh:
        json.dump({"first_load": False, "server_enabled": True, "server_port": PORT, "alerts_enabled": False,
                   "auth_required": True, "server_password": pw}, fh, indent=1)


def write_profile() -> str:
    d = os.path.join(CFG, "basic", "profiles", NAME)
    os.makedirs(d, exist_ok=True)
    os.makedirs(RUNS_OBS, exist_ok=True)
    w, h = CANVAS
    p = os.path.join(d, "basic.ini")
    _ini_update(p, {
        "General": {"Name": NAME},
        "Video": {"BaseCX": w, "BaseCY": h, "OutputCX": w, "OutputCY": h, "FPSType": 0, "FPSCommon": FPS,
                  "ScaleType": "lanczos", "ColorFormat": "NV12", "ColorSpace": "709", "ColorRange": "Partial"},
        "Audio": {"SampleRate": 48000, "ChannelSetup": "Stereo"},
        "Output": {"Mode": "Advanced", "FilenameFormatting": "TOD_%CCYY%MM%DD_%hh%mm%ss"},
        # forward slashes: OBS's ini reader unescapes "\n" (C:\Users\nicks -> newline)
        "AdvOut": {"RecType": "Standard", "RecFilePath": RUNS_OBS.replace("\\", "/"), "RecFormat2": "hybrid_mp4",
                   "RecEncoder": "obs_nvenc_h264_tex", "RecUseRescale": "false", "RecTracks": 1,
                   "RecAudioEncoder": "ffmpeg_aac", "RecSplitFile": "false", "Track1Bitrate": 192},
    })
    with open(os.path.join(d, "recordEncoder.json"), "w", encoding="utf-8") as fh:
        json.dump({"rate_control": "CQP", "cqp": CQP, "preset2": "p5", "preset": "p5", "tune": "hq",
                   "multipass": "qres", "keyint_sec": 2, "profile": "high", "bf": 2}, fh, indent=1)
    return p


def blank_png() -> str:
    """1x1 transparent PNG the tally Image source points at until the director retargets it to the run's
    tally/tally_current.png."""
    p = os.path.join(RUNS_OBS, "tally_blank.png")
    if not os.path.exists(p):
        from PIL import Image
        os.makedirs(RUNS_OBS, exist_ok=True)
        Image.new("RGBA", (1, 1), (0, 0, 0, 0)).save(p)
    return p


def _item(src: dict, box: tuple, iid: int, visible: bool = True) -> dict:
    x, y, bw, bh = box
    return {"name": src["name"], "source_uuid": src["uuid"], "visible": visible, "locked": False, "rot": 0.0,
            "pos": {"x": float(x), "y": float(y)}, "scale": {"x": 1.0, "y": 1.0}, "align": 5,
            "bounds_type": 2 if bw else 0, "bounds_align": 0, "bounds_crop": False,
            "bounds": {"x": float(bw), "y": float(bh)},
            "crop_left": 0, "crop_top": 0, "crop_right": 0, "crop_bottom": 0, "id": iid,
            "group_item_backup": False, "scale_filter": SCALE_FILTER.get(src["name"], "disable"),
            "blend_method": "default", "blend_type": "normal",
            "show_transition": {"duration": 0}, "hide_transition": {"duration": 0}, "private_settings": {}}


def _source(name: str, sid: str, settings: dict, audio: bool = False, vid: str | None = None) -> dict:
    return {"name": name, "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, "tod-obs/" + name)), "id": sid,
            "versioned_id": vid or sid, "settings": settings, "mixers": 255 if audio else 0, "sync": 0,
            "flags": 0, "volume": 1.0, "balance": 0.5, "enabled": True, "muted": False,
            "push-to-mute": False, "push-to-mute-delay": 0, "push-to-talk": False, "push-to-talk-delay": 0,
            "hotkeys": {}, "deinterlace_mode": 0, "deinterlace_field_order": 0, "monitoring_type": 0,
            "private_settings": {}, "filters": []}


def collection() -> dict:
    w, h = CANVAS
    src = {
        "bg": _source("bg", "color_source", {"color": BG_ABGR, "width": w, "height": h}, vid="color_source_v3"),
        "game": _source("game", "game_capture", {
            "capture_mode": "window", "window": GAME_WINDOW, "priority": 2, "capture_cursor": True,
            "allow_transparency": False, "anti_cheat_hook": True, "capture_overlays": False, "hook_rate": 1}),
        "game_wgc": _source("game_wgc", "window_capture", {
            "method": 2, "window": GAME_WINDOW, "priority": 2, "cursor": True, "client_area": True,
            "compatibility": False}),
        "sidebar": _source("sidebar", "window_capture", {
            "method": 2, "window": SIDEBAR_WINDOW, "priority": 1, "cursor": False, "client_area": True,
            "compatibility": False}),
        "tally": _source("tally", "image_source", {"file": blank_png(), "unload": False, "linear_alpha": False}),
        "game_audio": _source("game_audio", "wasapi_process_output_capture",
                              {"window": GAME_WINDOW, "priority": 2}, audio=True),
    }
    scenes = []
    for sname, boxes in SCENES.items():
        items, iid = [_item(src["bg"], (0, 0, 0, 0), 1)], 1
        for name in ("game", "game_wgc", "sidebar", "tally"):
            iid += 1
            box = boxes[name if name != "game_wgc" else "game"]
            items.append(_item(src[name], box, iid, visible=name != "game_wgc"))
        iid += 1
        items.append(_item(src["game_audio"], (0, 0, 0, 0), iid))
        sc = _source(sname, "scene", {"id_counter": iid, "custom_size": False, "items": items})
        scenes.append(sc)
    return {"name": NAME, "current_scene": "GAME", "current_program_scene": "GAME",
            "scene_order": [{"name": s["name"]} for s in scenes],
            "current_transition": TRANSITION, "transition_duration": TRANSITION_MS,
            "transitions": [{"name": TRANSITION, "id": "move_transition", "settings": {}}],
            "sources": list(src.values()) + scenes, "groups": [], "quick_transitions": [],
            "saved_projectors": [], "preview_locked": False, "scaling_enabled": False, "modules": {},
            "resolution": {"x": w, "y": h}, "version": 2}


def write_collection() -> str:
    d = os.path.join(CFG, "basic", "scenes")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{NAME}.json")
    with open(p + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(collection(), fh, indent=1)
    os.replace(p + ".tmp", p)
    return p


# ---------------------------------------------------------------- 3: launch
def obs_running() -> bool:
    import psutil
    return any((p.info["name"] or "").lower() == "obs64.exe" for p in psutil.process_iter(["name"]))


def connect(timeout: float = 3.0):
    """obsws-python ReqClient, or None when the websocket is not up."""
    import obsws_python as obs
    try:
        return obs.ReqClient(host=HOST, port=PORT, password=password(), timeout=timeout)
    except Exception:
        return None


def ready(cl) -> bool:
    """False while OBS answers 207 'not ready' (still loading the collection)."""
    try:
        cl.get_video_settings()
        return True
    except Exception:
        return False


def launch(wait_s: float = 60.0):
    log("launching OBS (tray, --collection TOD --profile TOD)")
    clear_sentinels()
    subprocess.Popen([OBS_EXE, "--collection", NAME, "--profile", NAME, "--minimize-to-tray", "--disable-updater",
                      "--disable-shutdown-check"], cwd=OBS_BIN,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while time.time() - t0 < wait_s:
        cl = connect()
        if cl and ready(cl):
            log(f"websocket up after {time.time() - t0:.1f}s")
            return cl
        time.sleep(1.0)
    raise SystemExit(f"OBS started but the websocket on {PORT} did not answer within {wait_s:.0f}s")


def clear_sentinels() -> None:
    """OBS 32 marks a run with %APPDATA%\\obs-studio\\.sentinel\\run_<uuid> and, when one is left behind (killed
    OBS), opens a modal "unclean shutdown / safe mode" dialog at the next launch -- which blocks the websocket.
    Only called while no obs64.exe runs."""
    d = os.path.join(CFG, ".sentinel")
    if os.path.isdir(d) and not obs_running():
        for f in os.listdir(d):
            if f.startswith("run_"):
                try:
                    os.remove(os.path.join(d, f))
                except OSError:
                    pass


def close_obs(timeout: float = 20.0) -> None:
    """WM_CLOSE to OBS's windows (a clean exit, ConfirmOnExit=false); kill only if that does not work."""
    import psutil
    import win32con
    import win32gui
    import win32process
    procs = [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "obs64.exe"]
    pids = {p.pid for p in procs}

    def cb(h, _):
        if win32process.GetWindowThreadProcessId(h)[1] in pids and win32gui.GetWindowText(h).startswith("OBS"):
            win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
    win32gui.EnumWindows(cb, None)
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        p.kill()
    if alive:
        psutil.wait_procs(alive, timeout=5)
    clear_sentinels()


# ---------------------------------------------------------------- 4: verify
def verify(cl, fix: bool = True) -> list[str]:
    """Problems found (empty = OK). Fixes transforms / transition / stray audio inputs when fix=True."""
    probs = []
    v = cl.get_video_settings()
    if (v.base_width, v.base_height, v.output_width, v.output_height) != (*CANVAS, *CANVAS) or \
            round(v.fps_numerator / v.fps_denominator) != FPS:
        probs.append(f"video {v.base_width}x{v.base_height}->{v.output_width}x{v.output_height} "
                     f"@{v.fps_numerator}/{v.fps_denominator}")
    scenes = {s["sceneName"] for s in cl.get_scene_list().scenes}
    for sname, boxes in SCENES.items():
        if sname not in scenes:
            probs.append(f"scene {sname} missing")
            continue
        for it in cl.get_scene_item_list(sname).scene_items:
            name = it["sourceName"]
            base = "game" if name == "game_wgc" else name
            if base not in boxes:
                continue
            x, y, bw, bh = boxes[base]
            tr = cl.get_scene_item_transform(sname, it["sceneItemId"]).scene_item_transform
            ok = (abs(tr["positionX"] - x) < 0.6 and abs(tr["positionY"] - y) < 0.6 and tr["boundsType"] ==
                  "OBS_BOUNDS_SCALE_INNER" and abs(tr["boundsWidth"] - bw) < 0.6 and
                  abs(tr["boundsHeight"] - bh) < 0.6 and tr["alignment"] == 5)
            if not ok:
                probs.append(f"{sname}/{name} transform {tr['positionX']},{tr['positionY']} "
                             f"{tr['boundsType']} {tr['boundsWidth']}x{tr['boundsHeight']}")
                if fix:
                    cl.set_scene_item_transform(sname, it["sceneItemId"], {
                        "positionX": x, "positionY": y, "alignment": 5, "boundsType": "OBS_BOUNDS_SCALE_INNER",
                        "boundsAlignment": 0, "boundsWidth": bw, "boundsHeight": bh, "rotation": 0,
                        "scaleX": 1.0, "scaleY": 1.0, "cropLeft": 0, "cropTop": 0, "cropRight": 0, "cropBottom": 0})
    tl = [t["transitionName"] for t in cl.get_scene_transition_list().transitions]
    want = TRANSITION if TRANSITION in tl else "Fade"
    if want != TRANSITION:
        probs.append(f"transition {TRANSITION} not loaded (plugin?) -- using Fade")
    cur = cl.get_current_scene_transition()
    if fix and (cur.transition_name != want or (cur.transition_duration or 0) != TRANSITION_MS):
        cl.set_current_scene_transition(want)
        cl.set_current_scene_transition_duration(TRANSITION_MS)
    sp = cl.get_special_inputs()
    for k in ("desktop1", "desktop2", "mic1", "mic2", "mic3", "mic4"):
        n = getattr(sp, k, None)
        if n:
            probs.append(f"special audio input {k}={n!r} present" + (" -> removed" if fix else ""))
            if fix:
                cl.remove_input(n)
    rd = cl.get_record_directory().record_directory
    if os.path.normcase(os.path.normpath(rd)) != os.path.normcase(RUNS_OBS):
        probs.append(f"record dir {rd}")
        if fix:
            cl.set_record_directory(RUNS_OBS)
    for sec, k, want_v in (("AdvOut", "RecFormat2", "hybrid_mp4"), ("AdvOut", "RecEncoder", "obs_nvenc_h264_tex"),
                           ("Output", "Mode", "Advanced")):
        got = cl.get_profile_parameter(sec, k).parameter_value
        if got != want_v:
            probs.append(f"profile {sec}.{k}={got!r} (want {want_v!r})")
    return probs


# ---------------------------------------------------------------- GPU preference (opt-in)
def game_exe() -> str | None:
    import psutil
    for p in psutil.process_iter(["name", "exe"]):
        if (p.info["name"] or "").lower() == "papersplease.exe" and p.info["exe"]:
            return p.info["exe"]
    for lib in (r"C:\Program Files (x86)\Steam\steamapps\common\Papers Please",
                r"C:\Program Files\Steam\steamapps\common\Papers Please"):
        if os.path.exists(os.path.join(lib, "PapersPlease.exe")):
            return os.path.join(lib, "PapersPlease.exe")
    return None


def gpu_prefs(write: bool) -> dict:
    import winreg
    key = r"Software\Microsoft\DirectX\UserGpuPreferences"
    out = {}
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
        for exe in (OBS_EXE, game_exe()):
            if not exe:
                continue
            try:
                cur = winreg.QueryValueEx(k, exe)[0]
            except OSError:
                cur = None
            if write and cur != "GpuPreference=2;":
                winreg.SetValueEx(k, exe, 0, winreg.REG_SZ, "GpuPreference=2;")
                cur = "GpuPreference=2; (written; restart the app)"
            out[exe] = cur
    return out


def setup(rewrite: bool = False, check_only: bool = False) -> object:
    """Idempotent: returns a connected ReqClient with a verified configuration."""
    if check_only:
        cl = connect()
        if not cl:
            raise SystemExit("OBS websocket not reachable")
        return cl
    ver = ensure_obs()
    dll = ensure_move()
    log(f"OBS {'.'.join(map(str, ver))}; Move transition {dll}")
    pw = password()
    cl = connect() if obs_running() else None
    for _ in range(30):
        if not cl or ready(cl):
            break
        time.sleep(1.0)
    if cl and not rewrite:
        log("OBS already running with the websocket up: verifying only")
    else:
        if obs_running():
            log("closing OBS to rewrite its config")
            close_obs()
        write_app_config(ver, pw)
        log(f"profile {write_profile()}")
        log(f"scene collection {write_collection()}")
        cl = launch()
    probs = verify(cl, fix=True)
    for p in probs:
        log("fixed/noted: " + p)
    left = verify(cl, fix=False)
    left = [p for p in left if "not loaded" not in p]
    if left:
        raise SystemExit("OBS config still off after fixes: " + "; ".join(left))
    log("OBS verified: 3840x2160@30, scenes " + " ".join(SCENES) + f", transition {TRANSITION} {TRANSITION_MS} ms")
    return cl


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rewrite", action="store_true", help="close OBS and rewrite every config file")
    ap.add_argument("--check", action="store_true", help="verify a running OBS (no installs, no writes)")
    ap.add_argument("--gpu-prefs", action="store_true",
                    help="write Windows Graphics 'High performance' for obs64.exe + PapersPlease.exe (HKCU)")
    a = ap.parse_args(argv)
    if a.gpu_prefs:
        for exe, v in gpu_prefs(write=True).items():
            log(f"GPU preference {exe}: {v}")
        return 0
    cl = setup(rewrite=a.rewrite, check_only=a.check)
    if a.check:
        probs = verify(cl, fix=False)
        for p in probs:
            log("PROBLEM: " + p)
        for exe, v in gpu_prefs(write=False).items():
            log(f"GPU preference {exe}: {v or 'unset (system default)'}")
        return 1 if probs else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
