"""patch_tally.py -- re-skin the TALLY scenes of a recorded demo video with re-exported tally PNGs (no retake).

    .venv-loop\\Scripts\\python.exe tools\\tally.py --run runs\\<ts> --export          # corrected PNGs first
    .venv-loop\\Scripts\\python.exe tools\\patch_tally.py --run runs\\<ts> [--video demo_day3.mp4]
        [--out demo_day3_final.mp4] [--final-day 3] [--dry-run]

What it does (docs/demo.md "Patching the tally in a finished video"):
  - TALLY spans from runs/<ts>/scene_log.json (the director's scene switches): from the TALLY switch to the next
    non-TALLY switch (the last one runs to the end of the video).
  - Inside a span the recorded frame is matched against the exported PNG variants (tally_day<d>_t<NNNN>.png: the
    live window repainted on every tick loaded while it was up; tally_final.png for the final day), ignoring the
    cost regions, which are the only parts that changed. Every static frame must match one variant, else abort.
  - The Move transition frames (tally sliding in from / out to below the canvas, ~0.7 s) are matched for their
    vertical offset, so the slide shows the new image too.
  - One ffmpeg pass: each variant scaled to the canvas exactly as the OBS TALLY scene does (1920x1080 image, bounds
    = full 3840x2160 canvas, scale 2.0, lanczos; tools/obs_setup.py TALLY_ON) and overlaid on its frames
    (enable=between(n,...), y per slide frame); video re-encoded with NVENC h264 CQP 18, audio stream-copied
    (untouched), chapters kept.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys

import numpy as np
from PIL import Image

FPS = 30
CW, CH = 3840, 2160          # OBS canvas (obs_setup.py)
DW, DH = 1920, 1080          # tally design / PNG size
SW, SH = 480, 270            # matching resolution for static frames
MOVE_S = 0.7                 # obs_setup.TRANSITION_MS
# design-px regions that changed with the cost model: cost hero tile, COST column (+ the old "~" left of it),
# notes line. Ignored when matching recorded frames to the new PNGs.
COST_BOXES = [(1470, 170, 1850, 410), (1370, 425, 1530, 880), (60, 785, 1880, 880)]


def mask(w: int, h: int, dy: int = 0) -> np.ndarray:
    m = np.ones((h, w), bool)
    for x0, y0, x1, y1 in COST_BOXES:
        m[int(y0 * h / DH):int(y1 * h / DH) + 1, int(x0 * w / DW):int(x1 * w / DW) + 1] = False
    return m


def frames(video: str, n0: int, n1: int, w: int, h: int, crop: str | None = None, gray: bool = False):
    """Decoded frames n0..n1-1 (indices of the CFR stream) at w x h; returns {n: array}."""
    vf = (f"crop={crop}," if crop else "") + f"scale={w}:{h}:flags=area"
    pix, ch = ("gray", 1) if gray else ("rgb24", 3)
    ss = max(0.0, (n0 - 15) / FPS)
    p = subprocess.run(["ffmpeg", "-v", "info", "-hide_banner", "-ss", f"{ss:.4f}", "-i", video, "-an", "-sn", "-dn",
                        "-vf", f"showinfo,{vf}", "-frames:v", str(n1 - n0 + 30),
                        "-f", "rawvideo", "-pix_fmt", pix, "-"], capture_output=True)
    pts = [ss + float(x) for x in re.findall(rb"pts_time:\s*(-?[\d.]+)", p.stderr)]
    a = np.frombuffer(p.stdout, np.uint8).reshape(-1, h, w, ch) if ch == 3 else \
        np.frombuffer(p.stdout, np.uint8).reshape(-1, h, w)
    out = {}
    for t, f in zip(pts, a):
        n = round(t * FPS)
        if n0 <= n < n1:
            out[n] = f.astype(np.float32)
    return out


def spans(scene_log: str, dur: float) -> list[dict]:
    ev = json.load(open(scene_log, encoding="utf-8"))["events"]
    out = []
    for i, e in enumerate(ev):
        if e["scene"] != "TALLY":
            continue
        nxt = next((x for x in ev[i + 1:] if x["scene"] != "TALLY"), None)
        m = re.search(r"day (\d+)", e.get("reason") or "")
        out.append({"t0": e["rec_t"], "t1": nxt["rec_t"] if nxt else dur, "end": nxt is None,
                    "day": int(m.group(1)) if m else None, "reason": e.get("reason")})
    return out


def variants(tally_dir: str, day: int | None, final_day: int) -> list[str]:
    if day is None or day >= final_day:
        return [os.path.join(tally_dir, "tally_final.png")]
    v = sorted(glob.glob(os.path.join(tally_dir, f"tally_day{day}_t*.png")))
    return v or [os.path.join(tally_dir, f"tally_day{day}.png")]


def plan(a) -> dict:
    video = os.path.join(a.run, a.video)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video],
                               capture_output=True, text=True).stdout.strip())
    nmax = round(dur * FPS)
    tally_dir = os.path.join(a.run, "tally")
    ms = mask(SW, SH)
    items = []   # (png, kind, n, dy)  kind: static | slide
    report = []
    for sp in spans(os.path.join(a.run, "scene_log.json"), dur):
        pngs = variants(tally_dir, sp["day"], a.final_day)
        V = {p: np.asarray(Image.open(p).convert("RGB").resize((SW, SH), Image.BILINEAR), np.float32) for p in pngs}
        n_in, n_out = round(sp["t0"] * FPS), min(nmax, round(sp["t1"] * FPS))
        # static part: after the slide-in until the next switch
        F = frames(video, n_in, n_out, SW, SH)
        segs, first_static = [], None
        for n in sorted(F):
            err = {p: float(np.abs(F[n] - v)[ms].mean()) for p, v in V.items()}
            p = min(err, key=err.get)
            if err[p] > a.max_err:
                if n >= n_in + round((MOVE_S + 0.3) * FPS):
                    sys.exit(f"frame {n} ({n / FPS:.3f}s) in TALLY span {sp['reason']!r} matches no PNG "
                             f"(best {os.path.basename(p)} err {err[p]:.2f}); re-export with tools/tally.py --export")
                continue   # still sliding in
            first_static = n if first_static is None else first_static
            if segs and segs[-1][0] == p and segs[-1][2] == n - 1:
                segs[-1][2] = n
            else:
                segs.append([p, n, n])
        if not segs:
            sys.exit(f"TALLY span {sp['reason']!r}: no frame matched a PNG")
        for p, s0, s1 in segs:
            items.append((p, "static", s0, s1))
        # slide frames: in (switch .. first static frame), out (next switch .. +0.7 s + margin)
        slides = [(n_in, first_static, segs[0][0])]
        if not sp["end"]:
            slides.append((n_out, min(nmax, n_out + round((MOVE_S + 0.4) * FPS)), segs[-1][0]))
        n_slide = 0
        for s0, s1, p in slides:
            if s1 <= s0:
                continue
            # tally slides vertically (Move: TALLY_OFF y=2160 <-> TALLY_ON y=0): find its y per frame, coarse at
            # 1/8 then +-8 px on a full-res strip; frames with < 160 canvas rows of tally visible only show its
            # unchanged olive header, nothing to patch there
            big = np.asarray(Image.open(p).convert("L").resize((CW, CH), Image.LANCZOS), np.float32)
            q = 8
            G = frames(video, s0, s1, CW // q, CH // q, gray=True)
            Gs = frames(video, s0, s1, 800, CH, crop=f"800:{CH}:200:0", gray=True)
            sq = np.asarray(Image.open(p).convert("L").resize((CW // q, CH // q), Image.BILINEAR), np.float32)
            mq, mfull = mask(CW // q, CH // q), mask(CW, CH)[:, 200:1000]
            hq = CH // q
            for n in sorted(G):
                best = None
                for dq in range(0, hq - 160 // q):
                    d = float(np.abs(G[n][dq:] - sq[:hq - dq])[mq[:hq - dq]].mean())
                    if best is None or d < best[0]:
                        best = (d, dq)
                if best is None or best[0] > a.max_err * 2:
                    continue
                rb = None
                for dy in range(max(0, best[1] * q - 8), min(CH - 160, best[1] * q + 9)):
                    d = float(np.abs(Gs[n][dy:] - big[:CH - dy, 200:1000])[mfull[:CH - dy]].mean())
                    if rb is None or d < rb[0]:
                        rb = (d, dy)
                items.append((p, "slide", n, rb[1]))
                n_slide += 1
        report.append({"span": sp["reason"], "t0": sp["t0"], "t1": sp["t1"], "slide_frames": n_slide,
                       "segments": [(os.path.basename(p), round(s0 / FPS, 3), round((s1 + 1) / FPS, 3))
                                    for p, s0, s1 in segs]})
    return {"video": video, "dur": dur, "items": items, "report": report}


def nvenc_ok() -> bool:
    """h264_nvenc opens on this machine? (ffmpeg 9 builds need NVIDIA driver >= 610; older drivers fail to open.)"""
    r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=256x256:d=0.1", "-c:v", "h264_nvenc",
                        "-f", "null", "-"], capture_output=True)
    return r.returncode == 0


def venc() -> list[str]:
    """Video encoder args at constant QP 18: NVENC if it works, else libx264 (same QP, slower)."""
    if nvenc_ok():
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "constqp", "-qp", "18",
                "-profile:v", "high"]
    print("[patch] h264_nvenc unavailable (driver too old for this ffmpeg build) -> libx264 -qp 18")
    return ["-c:v", "libx264", "-preset", "veryfast", "-qp", "18", "-profile:v", "high"]


def ffmpeg_cmd(P: dict, out: str) -> tuple[list[str], str]:
    pngs = sorted({it[0] for it in P["items"]})
    fc = [f"[0:v]null[v0]"]
    cur = "v0"
    for i, p in enumerate(pngs, 1):
        st = [it for it in P["items"] if it[0] == p and it[1] == "static"]
        sl = [it for it in P["items"] if it[0] == p and it[1] == "slide"]
        en = "+".join([f"between(n,{s0},{s1})" for _, _, s0, s1 in st] + [f"eq(n,{n})" for _, _, n, _ in sl])
        y = "0"
        for _, _, n, dy in sl:
            y = f"if(eq(n,{n}),{dy},{y})"
        # 1920x1080 RGB -> canvas (bounds 3840x2160, scale 2.0, lanczos as the OBS TALLY scene), bt709 limited
        fc.append(f"[{i}:v]scale={CW}:{CH}:flags=lanczos:out_color_matrix=bt709:out_range=tv,format=yuv420p[t{i}]")
        fc.append(f"[{cur}][t{i}]overlay=x=0:y='{y}':eval=frame:eof_action=repeat:enable='{en}'[v{i}]")
        cur = f"v{i}"
    graph = ";".join(fc)
    cmd = ["ffmpeg", "-y", "-v", "error", "-stats", "-i", P["video"]]
    for p in pngs:
        cmd += ["-i", p]
    # ffmpeg >= 7: -/filter_complex <file> (-filter_complex_script is gone)
    cmd += ["-/filter_complex", "{graph}", "-map", f"[{cur}]", "-map", "0:a", "-map_chapters", "0",
            *venc(), "-g", "60", "-bf", "2", "-pix_fmt", "yuv420p", "-color_range", "tv", "-colorspace", "bt709",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-fps_mode", "passthrough",
            "-c:a", "copy", "-movflags", "+faststart", out]
    return cmd, graph


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--video", default="demo_day3.mp4", help="recorded video in the run dir")
    ap.add_argument("--out", default=None, help="default: <video>_final.mp4 in the run dir")
    ap.add_argument("--final-day", type=int, default=3)
    ap.add_argument("--max-err", type=float, default=8.0, help="mean abs RGB diff for a frame to match a PNG")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and the ffmpeg command only")
    a = ap.parse_args(argv)
    out = a.out or os.path.join(a.run, os.path.splitext(a.video)[0] + "_final.mp4")
    P = plan(a)
    for r in P["report"]:
        print(f"[patch] {r['span']}: switch {r['t0']:.2f}s .. {r['t1']:.2f}s, {r['slide_frames']} slide frames")
        for name, s0, s1 in r["segments"]:
            print(f"          {s0:8.3f} - {s1:8.3f}  {name}")
    cmd, graph = ffmpeg_cmd(P, out)
    gpath = os.path.join(a.run, "tally", "patch_filter.txt")
    with open(gpath, "w", encoding="utf-8") as fh:
        fh.write(graph)
    cmd[cmd.index("{graph}")] = gpath
    with open(os.path.join(a.run, "tally", "patch_plan.json"), "w", encoding="utf-8") as fh:
        json.dump({"report": P["report"], "items": [(os.path.basename(p), k, s, e) for p, k, s, e in P["items"]],
                   "cmd": cmd}, fh, indent=1)
    print("[patch] " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    if a.dry_run:
        return 0
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    sys.exit(main())
