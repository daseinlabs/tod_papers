"""overlay.py -- probability heatmap over the annotated SoM frame.

Each box is filled red with alpha proportional to P(source=id) and outlined
blue with thickness proportional to P(target=id). A banner across the top
states the executed action and its effect; a side legend lists the chosen
action, the top-3 source alternatives (with descriptions and probabilities),
and the action/screen/target/goal distributions.
"""
from __future__ import annotations

import cv2
import numpy as np


def _fit(t: str, n: int) -> str:
    return t if len(t) <= n else t[: n - 3] + "..."


def render(annotated: np.ndarray, idmap: dict, res, path: str, descriptions: dict | None = None,
           executed: str = "", effect: str = "") -> None:
    descriptions = descriptions or {}
    img = annotated.copy()
    H, W = img.shape[:2]
    ps = res["source"].probabilities
    pt = res["target"].probabilities
    pmax = max(ps.values()) if ps else 1.0
    heat = np.zeros_like(img)
    alpha = np.zeros((H, W), np.float32)
    for i, b in idmap.items():
        if b.kind == "background":
            continue
        p = ps.get(str(i), 0.0) / max(pmax, 1e-6)
        heat[b.y1:b.y2, b.x1:b.x2] = (0, 0, 255)
        alpha[b.y1:b.y2, b.x1:b.x2] = np.maximum(alpha[b.y1:b.y2, b.x1:b.x2], 0.65 * p)
    a = alpha[..., None]
    img = (img * (1 - a) + heat * a).astype(np.uint8)
    for i, b in idmap.items():
        q = pt.get(str(i), 0.0)
        if q > 0.05:
            cv2.rectangle(img, (b.x1, b.y1), (b.x2, b.y2), (255, 120, 0), max(1, int(14 * q)))
    # chosen element: thick yellow frame; drag target: cyan frame + arrow
    src = str(res["source"].value)
    tgt = str(res["target"].value)
    act = res["action"].value
    sb = idmap.get(int(src)) if src.isdigit() else None
    tb = idmap.get(int(tgt)) if tgt.isdigit() else None
    if sb is not None and act != "wait":
        cv2.rectangle(img, (sb.x1, sb.y1), (sb.x2, sb.y2), (0, 255, 255), 6)
    if act == "drag" and sb is not None and tb is not None:
        cv2.rectangle(img, (tb.x1, tb.y1), (tb.x2, tb.y2), (255, 255, 0), 6)
        cv2.arrowedLine(img, sb.center, tb.center, (0, 255, 255), 6, cv2.LINE_AA, tipLength=0.04)

    k = H / 1280
    fs = 0.8 * k + 0.2
    # banner with the chosen action
    p_src = ps.get(src, 0.0)
    if act == "drag":
        chosen = f"{act.upper()} #{src} ({p_src:.2f}) -> #{tgt} ({pt.get(tgt, 0.0):.2f}): {_fit(descriptions.get(src, ''), 60)}"
    elif act == "click":
        chosen = f"CLICK #{src} ({p_src:.2f}): {_fit(descriptions.get(src, ''), 80)}"
    else:
        chosen = "WAIT"
    bh = int(90 * k)
    banner = np.full((bh, W, 3), 20, np.uint8)
    cv2.putText(banner, _fit(chosen, 110), (12, int(38 * k)), cv2.FONT_HERSHEY_SIMPLEX, fs, (0, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(banner, _fit(f"executed: {executed}   effect: {effect}", 120), (12, int(78 * k)),
                cv2.FONT_HERSHEY_SIMPLEX, fs * 0.85, (200, 200, 200), 2, cv2.LINE_AA)
    img = np.vstack([banner, img])
    H2 = img.shape[0]

    # legend panel
    panel = np.full((H2, 640, 3), 24, np.uint8)
    y = 40

    def put(t, col=(230, 230, 230), scale=1.0):
        nonlocal y
        cv2.putText(panel, _fit(t, 44), (12, y), cv2.FONT_HERSHEY_SIMPLEX, fs * scale, col, 2, cv2.LINE_AA)
        y += int((38 * k + 6) * scale)

    put(f"action: {act}", (120, 220, 255))
    for lab, v in sorted(res["action"].probabilities.items(), key=lambda kv: -kv[1])[:3]:
        put(f"   {lab}: {v:.3f}")
    put("source top-3:", (120, 220, 255))
    for n, (lab, v) in enumerate(sorted(ps.items(), key=lambda kv: -kv[1])[:3]):
        put(f" {'*' if n == 0 else ' '}#{lab}: {v:.3f}", (0, 255, 255) if n == 0 else (230, 230, 230))
        put(f"     {descriptions.get(lab, '')}", (170, 170, 170), 0.75)
    for q in ("target", "screen"):
        a_ = res[q]
        put(f"{q}: {a_.value}", (120, 220, 255))
        for lab, v in sorted(a_.probabilities.items(), key=lambda kv: -kv[1])[:3]:
            put(f"   {lab}: {v:.3f}")
    try:
        g = res["goal"].probabilities
        put(f"goal (entrant done) p(yes)={g.get('true', 0.0):.2f}", (120, 220, 255))
    except KeyError:
        pass
    cv2.imwrite(path, np.hstack([img, panel]))
