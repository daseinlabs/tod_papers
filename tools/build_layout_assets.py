"""Build src/tod_papers/layout_assets.npz from saved frames (offline, no game input).

The static layout (layout.py) needs a few exact pixel references, all at the
native 570x320 art resolution:
  * ref_booth      -- a full native booth frame with an empty counter, the rulebook in
                      its drawer and nothing in the bulletin-storage tray
                      (runs/20261002_003519/raw_0000.png). Used for the counter /
                      drawer-row reference diff and the booth probes.
  * tpl_<name>     -- binary text/icon masks (gray > 40) of the menu buttons, cut at
                      their fixed slots (layout.MENU_SLOTS) from runs/20261002_080555.

Re-run after a game update that changes the art:
    .venv-loop/Scripts/python tools/build_layout_assets.py
"""
from __future__ import annotations

import os
import sys

import cv2
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from tod_papers.layout import ASSETS_FILE, MENU_SLOTS, to_native  # noqa: E402

BOOTH = os.path.join(ROOT, "runs", "20261002_003519", "raw_0000.png")
MENU = os.path.join(ROOT, "runs", "20261002_080555", "raw_%04d.png")

# template name -> (menu-run frame index, slot name)
TEMPLATES = {
    "story": (1, "story"),
    "endless": (1, "endless"),
    "quit": (1, "quit"),
    "settings": (1, "settings"),
    "select_header": (2, "select_header"),
    "day1_new": (2, "day1_tile"),
    "trash": (2, "trash"),
    "back": (2, "bottom_button"),
    "next": (5, "bottom_button"),
    "walk_to_work": (13, "bottom_button"),
}


def main() -> int:
    out = {}
    booth = cv2.imread(BOOTH)
    if booth is None:
        print("missing", BOOTH)
        return 1
    out["ref_booth"] = to_native(booth)
    for name, (idx, slot) in TEMPLATES.items():
        f = cv2.imread(MENU % idx)
        if f is None:
            print("missing", MENU % idx)
            return 1
        x1, y1, x2, y2 = MENU_SLOTS[slot]
        out["tpl_" + name] = (to_native(f)[y1:y2, x1:x2].max(2) > 40).astype(np.uint8)
    np.savez_compressed(ASSETS_FILE, **out)
    print("wrote", ASSETS_FILE, {k: v.shape for k, v in out.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
