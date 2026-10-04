"""Behind-the-scenes panel for screen recording (docs/viewer.md).

Reads a run directory the loop writes (`tick_NNNN.json` + `tick_NNNN.png`, the SoM frame of request 2) and
renders one tall panel image per tick with PIL. The Tk window in `tools/viewer.py` only shows that image, so
the same renderer serves the live window, replay and PNG export. Nothing here talks to the loop or the game:
it polls files, read-only, so it has no effect on a running loop.
"""
from __future__ import annotations

import glob
import json
import os
import re

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- theme
BG = (18, 20, 24)
CARD = (30, 33, 40)
FG = (232, 234, 238)
DIM = (150, 156, 168)
ACCENT = (110, 180, 255)
GREEN = (80, 200, 120)
RED = (235, 90, 80)
AMBER = (240, 180, 60)
CHANGED_BG = (70, 58, 20)

FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")


def _font(name: str, size: int):
    for n in (name, "segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(os.path.join(FONT_DIR, n), size)
        except OSError:
            try:
                return ImageFont.truetype(n, size)
            except OSError:
                pass
    return ImageFont.load_default()


# ---------------------------------------------------------------- run directory access
TICK_RE = re.compile(r"tick_(\d{4,})\.json$")


def list_ticks(run_dir: str) -> list[int]:
    out = []
    for e in os.scandir(run_dir):
        m = TICK_RE.match(e.name)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def newest_run(runs_root: str) -> str | None:
    dirs = [d for d in glob.glob(os.path.join(runs_root, "2*")) if os.path.isdir(d)]
    return max(dirs) if dirs else None


class RunReader:
    """Cached, retrying reader. A json the loop is still writing fails to parse -> None, retried next poll."""

    def __init__(self, run_dir: str):
        self.run_dir = run_dir
        self.cache: dict[int, dict] = {}

    def rec(self, tick: int) -> dict | None:
        if tick in self.cache:
            return self.cache[tick]
        p = os.path.join(self.run_dir, f"tick_{tick:04d}.json")
        try:
            with open(p, encoding="utf-8") as fh:
                r = json.load(fh)
        except (OSError, ValueError):
            return None
        self.cache[tick] = r
        return r

    def som(self, tick: int):
        """The SoM frame of request 2 (falls back to the raw frame). None while the png is still being written."""
        for name in (f"tick_{tick:04d}.png", f"raw_{tick:04d}.png"):
            p = os.path.join(self.run_dir, name)
            if os.path.exists(p):
                img = cv2.imread(p)
                if img is not None:
                    return img
        return None


# ---------------------------------------------------------------- field extraction
def _yn(v) -> str:
    if v is True:
        return "yes"
    if v is False:
        return "no"
    return str(v)


def _pfmt(p) -> str:
    return f"{p:.2f}" if isinstance(p, (int, float)) else ""


def state_rows(rec: dict) -> list[tuple[str, str, float | None, str]]:
    """(key, label, p, value) for the request-1 answers TOD gave this tick."""
    st = rec.get("state") or {}
    rows = []

    def add(key, label, fmt=_yn):
        a = st.get(key)
        if a is not None:
            rows.append((key, label, a.get("p"), fmt(a.get("value"))))

    add("screen", "Screen", lambda v: str(v).replace("_", " "))
    add("person_at_window", "Person at booth")
    add("document_on_counter_shelf", "On counter")
    add("document_open_on_desk", "Paper on desk")
    add("stamp_tray_open", "Stamp tray open")
    pa, pd = st.get("passport_under_approved"), st.get("passport_under_denied")
    if pa or pd:
        if pa and pa.get("value"):
            rows.append(("passport_under", "Passport under", pa.get("p"), "APPROVED stamp"))
        elif pd and pd.get("value"):
            rows.append(("passport_under", "Passport under", pd.get("p"), "DENIED stamp"))
        else:
            ps = [x.get("p") for x in (pa, pd) if x and isinstance(x.get("p"), (int, float))]
            rows.append(("passport_under", "Passport under", min(ps) if ps else None, "no stamp"))
    add("passport_stamp_ink", "Ink on passport", lambda v: str(v).upper() if v != "none" else "none")
    add("issuing_country", "Country")
    add("exp_read", "EXP. date", str)
    add("issuing_city", "ISS. city", str)
    add("entry_ticket_dated_today", "Entry ticket", lambda v: str(v).replace("_", " "))
    add("ticket_date", "Ticket date", str)
    add("no_documents_presented", "No papers given")
    add("inspect_mode_on", "Inspect mode")
    add("interrogate_prompt_visible", "Interrogate btn")
    add("rulebook_page", "Rulebook page", lambda v: str(v).replace("_", " "))
    add("passport_returned", "Passport back", lambda v: str(v).replace("_", " "))
    add("day", "Day", str)
    return rows


def papers_line(rec: dict) -> str:
    """Paper identities (request 1b) grouped by place: 'desk: rulebook .75, flyer .84 x2 | counter: unread .20'."""
    groups: dict[str, list[str]] = {}
    for d in rec.get("docs_named") or []:
        p = d.get("p")
        s = f"{d.get('id')} {p:.2f}".replace(" 0.", " .") if isinstance(p, (int, float)) else str(d.get("id"))
        groups.setdefault(d.get("where") or "?", []).append(s)
    parts = []
    for where, items in groups.items():
        seen: dict[str, int] = {}
        for s in items:
            seen[s] = seen.get(s, 0) + 1
        parts.append(f"{where}: " + ", ".join(s + (f" x{n}" if n > 1 else "") for s, n in seen.items()))
    return " | ".join(parts)


def _section(text: str, head: str) -> list[str]:
    i = text.find(head)
    if i < 0:
        return []
    out = []
    for ln in text[i + len(head):].splitlines()[1:]:
        if not ln.strip():
            break
        out.append(ln.strip())
    return out


def situation(rec: dict) -> list[str]:
    """The dynamic 'WHAT APPLIES NOW' sentence(s) sent to TOD with request 2."""
    return [ln.lstrip("- ") for ln in _section(rec.get("state_text") or "", "WHAT APPLIES NOW")]


def rule_line(rec: dict) -> str:
    t = rec.get("state_text") or ""
    m = re.search(r"^(Day \d+ \(\d{4}\.\d\d\.\d\d\):.*)$", t, re.M)
    return m.group(1).strip() if m else ""


def verdict(rec: dict):
    v = (rec.get("state") or {}).get("verdict")
    if v:
        return v.get("probs") or {v.get("value"): v.get("p")}, "asked this tick"
    ev = (rec.get("entrant") or {}).get("verdict")
    if ev:
        return {str(ev).lower(): None}, "stored from an earlier tick"
    return None, "not asked this tick"


def pick(rec: dict) -> dict:
    ans = rec.get("answers") or {}
    desc = rec.get("descriptions") or {}
    src = ans.get("source") or {}
    probs = src.get("probabilities") or {}
    top = sorted(probs.items(), key=lambda kv: -kv[1])[:3]
    act = (ans.get("action") or {}).get("probabilities") or {}
    tgt = ans.get("target") or {}
    tgt_top = sorted((tgt.get("probabilities") or {}).items(), key=lambda kv: -kv[1])[:1]
    notes = []
    ex = str(rec.get("executed") or "")
    if ex.startswith("vetoed"):
        notes.append(ex)
    for k in ("convention_mismatch", "input_convention", "cycle_break", "repeat_drag_ban", "ban_lifted",
              "stop_reason"):
        if rec.get(k):
            notes.append(f"{k.replace('_', ' ')}: {rec[k]}")
    if rec.get("excluded"):
        notes.append("excluded: " + ", ".join(f"#{k}" for k in rec["excluded"]))
    return {
        "top": [(k, p, "WAIT" if k == "wait" else desc.get(k, "")) for k, p in top],
        "action": sorted(act.items(), key=lambda kv: -kv[1]),
        "chosen": rec.get("tod_pick"),
        "target": [(k, p, desc.get(k, "")) for k, p in tgt_top] if (rec.get("tod_pick") or [""])[0] == "drag" else [],
        "executed": ex,
        "notes": notes,
    }


def _short_desc(d: str, n: int = 22) -> str:
    d = re.sub(r"^(object|drop target)\s*[-\u2014]\s*", "", d or "")
    d = re.split(r"\s+[-\u2014]{1,2}\s+|\s\(", d)[0]
    return d if len(d) <= n else d[: n - 1] + "\u2026"


def history_item(rec: dict) -> tuple[str, str, object]:
    pk = rec.get("tod_pick") or ["", ""]
    desc = rec.get("descriptions") or {}
    ex = str(rec.get("executed") or "")
    if ex.startswith("vetoed"):
        label = "refused"
    elif pk[0] == "wait" or ex == "wait":
        label = "wait"
    else:
        label = f"{pk[0]} {_short_desc(desc.get(str(pk[1]), '#' + str(pk[1])), 24)}"
    return f"t{rec.get('tick')}", label, rec.get("changed")


# ---------------------------------------------------------------- rendering
class Panel:
    def __init__(self, width: int = 1160, height: int = 1980):
        self.W, self.H = width, height
        s = width / 1160.0
        self.s = s
        self.f_title = _font("seguisb.ttf", int(30 * s))
        self.f_head = _font("segoeuib.ttf", int(30 * s))
        self.f_body = _font("segoeui.ttf", int(28 * s))
        self.f_bold = _font("segoeuib.ttf", int(27 * s))
        self.f_small = _font("segoeui.ttf", int(24 * s))
        self.f_mono = _font("consola.ttf", int(25 * s))
        self.f_tiny = _font("segoeui.ttf", int(21 * s))
        self.pad = int(14 * s)
        self.FOOTER_H = int(100 * s)

    # -- helpers
    def _wrap(self, d, text, font, width, max_lines=3):
        words, lines, cur = text.split(), [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if d.textlength(t, font=font) <= width:
                cur = t
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            while lines[-1] and d.textlength(lines[-1] + "\u2026", font=font) > width:
                lines[-1] = lines[-1][:-1]
            lines[-1] += "\u2026"
        return lines

    def _fit(self, d, text, font, width):
        if d.textlength(text, font=font) <= width:
            return text
        while text and d.textlength(text + "\u2026", font=font) > width:
            text = text[:-1]
        return text + "\u2026"

    def _head(self, d, y, title, right=""):
        d.text((self.pad, y), title, font=self.f_head, fill=ACCENT)
        if right:
            w = d.textlength(right, font=self.f_small)
            d.text((self.W - self.pad - w, y + int(4 * self.s)), right, font=self.f_small, fill=DIM)
        return y + int(42 * self.s)

    def _bar(self, d, x, y, w, h, frac, color):
        d.rounded_rectangle((x, y, x + w, y + h), radius=int(4 * self.s), fill=(50, 54, 64))
        if frac and frac > 0:
            d.rounded_rectangle((x, y, x + max(int(w * min(frac, 1.0)), 2), y + h), radius=int(4 * self.s),
                                fill=color)

    # -- main
    def render(self, rec: dict, prev: dict | None, history: list[dict], som, run_name: str = "",
               mode: str = "") -> Image.Image:
        """Full-width SoM frame; if the text below would run into the footer, shrink the frame to make room."""
        img = self._render(rec, prev, history, som, run_name, mode, None)
        over = self.content_bottom - (self.H - self.FOOTER_H - int(8 * self.s))
        if over > 0 and som is not None:
            fw = self.W - 2 * self.pad
            fh = som.shape[0] * fw / som.shape[1]
            img = self._render(rec, prev, history, som, run_name, mode, int(fw * max(0.4, (fh - over) / fh)))
        return img

    def _render(self, rec, prev, history, som, run_name, mode, som_w) -> Image.Image:
        W, s, pad = self.W, self.s, self.pad
        img = Image.new("RGB", (W, self.H), BG)
        d = ImageDraw.Draw(img)
        y = int(8 * s)
        title = "TOD plays Papers, Please \u2014 behind the scenes"
        d.text((pad, y), title, font=self.f_title, fill=FG)
        tag = f"{run_name}  tick {rec.get('tick')}".strip()
        d.text((W - pad - d.textlength(tag, font=self.f_title), y), tag, font=self.f_title, fill=AMBER)
        y += int(46 * s)

        # (a) SoM frame
        y = self._head(d, y, "What TOD was shown (numbered options, request 2)")
        if som is not None:
            fw = som_w or (W - 2 * pad)
            fh = int(round(som.shape[0] * fw / som.shape[1]))
            small = cv2.resize(som, (fw, fh), interpolation=cv2.INTER_AREA)
            img.paste(Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)), ((W - fw) // 2, y))
            y += fh + int(10 * s)
        else:
            y += int(60 * s)

        # (b) What TOD sees
        y = self._head(d, y, "What TOD sees (request 1 answers, p)", "highlight = changed since last tick")
        prow = {k: v for k, _, _, v in state_rows(prev)} if prev else {}
        rows = state_rows(rec)
        colw = (W - 2 * pad - int(10 * s)) // 2
        rh = int(38 * s)
        for i, (k, label, p, val) in enumerate(rows):
            cx = pad + (i % 2) * (colw + int(10 * s))
            cy = y + (i // 2) * rh
            changed = prev is not None and prow.get(k) != val
            d.rounded_rectangle((cx, cy, cx + colw, cy + rh - int(3 * s)), radius=int(5 * s),
                                fill=CHANGED_BG if changed else CARD)
            d.text((cx + int(8 * s), cy + int(2 * s)), label, font=self.f_small, fill=DIM)
            vx = cx + int(200 * s)
            pt = _pfmt(p)
            pw = d.textlength(pt, font=self.f_mono)
            vcol = AMBER if changed else FG
            d.text((vx, cy), self._fit(d, val, self.f_bold, colw - int(210 * s) - pw - int(8 * s)),
                   font=self.f_bold, fill=vcol)
            d.text((cx + colw - pw - int(8 * s), cy + int(3 * s)), pt, font=self.f_mono, fill=DIM)
        y += ((len(rows) + 1) // 2) * rh + int(4 * s)
        pl = papers_line(rec)
        if pl:
            for ln in self._wrap(d, "Papers: " + pl, self.f_small, W - 2 * pad, 2):
                d.text((pad, y), ln, font=self.f_small, fill=FG)
                y += int(30 * s)
        y += int(8 * s)

        # (c) situation + rule
        y = self._head(d, y, "Situation sentence sent to TOD")
        sit = situation(rec) or ["(none this tick)"]
        left = 4
        for sent in sit[:2]:
            if left <= 0:
                break
            lines = self._wrap(d, sent, self.f_body, W - 2 * pad, left)
            left -= len(lines)
            for ln in lines:
                d.text((pad, y), ln, font=self.f_body, fill=FG)
                y += int(35 * s)
        rl = rule_line(rec)
        if rl:
            y += int(4 * s)
            for ln in self._wrap(d, "Rule today: " + rl, self.f_small, W - 2 * pad, 2):
                d.text((pad, y), ln, font=self.f_small, fill=DIM)
                y += int(29 * s)
        y += int(10 * s)

        # (d) verdict
        probs, how = verdict(rec)
        y = self._head(d, y, "TOD's verdict", how)
        if probs:
            items = [("approved", "APPROVED", GREEN), ("denied", "DENIED", RED),
                     ("cannot_decide_yet", "cannot decide yet", (120, 126, 140))]
            items = [it for it in items if it[0] in probs]
            best = max(probs, key=lambda k: probs[k] or 0)
            bw, bh = W - 2 * pad, int(34 * s)
            x0 = pad
            tot = sum((probs[k] if probs[k] is not None else 1.0) for k, _, _ in items) or 1.0
            for key, label, col in items:
                p = probs[key] if probs[key] is not None else 1.0
                w = int(bw * p / tot)
                if w > 0:
                    d.rectangle((x0, y, x0 + w, y + bh), fill=col)
                    t = f"{label} {_pfmt(probs[key])}" if probs[key] is not None else f"{label} (stored)"
                    if d.textlength(t, font=self.f_bold) < w - 8:
                        d.text((x0 + int(6 * s), y + int(2 * s)), t, font=self.f_bold, fill=(10, 10, 10))
                x0 += w
            if best in probs:
                d.rectangle((pad, y, pad + bw, y + bh), outline=(200, 200, 200), width=1)
            y += bh + int(4 * s)
            leg = "   ".join(f"{lab} {_pfmt(probs[k]) or 'stored'}" for k, lab, _ in items)
            d.text((pad, y), self._fit(d, leg, self.f_small, W - 2 * pad), font=self.f_small, fill=DIM)
            y += int(30 * s)
        else:
            d.text((pad, y), "—", font=self.f_body, fill=DIM)
            y += int(33 * s)
        y += int(8 * s)

        # (e) pick
        pk = pick(rec)
        act = "  ".join(f"{a} {p:.2f}" for a, p in pk["action"])
        y = self._head(d, y, "TOD's pick", f"action: {act}" if act else "")
        bx, bw, bh = pad + int(70 * s), int(160 * s), int(22 * s)
        chosen_id = str((pk["chosen"] or ["", ""])[1]) if pk["chosen"] else ""
        for k, p, desc in pk["top"]:
            is_c = str(k) == chosen_id
            d.text((pad, y), f"#{k}" if k != "wait" else "wait", font=self.f_bold, fill=AMBER if is_c else FG)
            self._bar(d, bx, y + int(6 * s), bw, bh, p, ACCENT if is_c else (90, 100, 120))
            d.text((bx + bw + int(8 * s), y + int(3 * s)), f"{p:.2f}", font=self.f_mono, fill=FG)
            tx = bx + bw + int(74 * s)
            dl = self._wrap(d, desc, self.f_small, W - pad - tx, 2 if is_c else 1)
            for j, ln in enumerate(dl):
                d.text((tx, y + int(3 * s) + j * int(27 * s)), ln, font=self.f_small, fill=FG if is_c else DIM)
            y += int(36 * s) + (len(dl) - 1) * int(27 * s)
        for k, p, desc in pk["target"]:
            ln = self._fit(d, f"drop on #{k} ({p:.2f}): {desc}", self.f_small, W - 2 * pad)
            d.text((pad, y), ln, font=self.f_small, fill=FG)
            y += int(28 * s)
        ex = pk["executed"]
        col = RED if ex.startswith("vetoed") else GREEN
        for ln in self._wrap(d, "Executed: " + ex, self.f_mono, W - 2 * pad, 2):
            d.text((pad, y), ln, font=self.f_mono, fill=col)
            y += int(27 * s)
        for n in pk["notes"][:3]:
            if n.startswith("vetoed"):
                continue
            d.text((pad, y), self._fit(d, n, self.f_small, W - 2 * pad), font=self.f_small, fill=AMBER)
            y += int(26 * s)
        y += int(10 * s)

        # (f) history strip
        y = self._head(d, y, "Last 8 actions", "green = screen changed, red = no change")
        n = 8
        cw = (W - 2 * pad - (n - 1) * int(6 * s)) // n
        ch = int(92 * s)
        hist = history[-n:]
        for i, h in enumerate(hist):
            t, label, eff = history_item(h)
            cx = pad + i * (cw + int(6 * s))
            col = (35, 80, 50) if eff is True else (95, 40, 38) if eff is False else CARD
            d.rounded_rectangle((cx, y, cx + cw, y + ch), radius=int(5 * s), fill=col,
                                outline=AMBER if h is rec else None, width=max(1, int(2 * s)))
            d.text((cx + int(6 * s), y + int(2 * s)), t, font=self.f_small, fill=FG)
            for j, ln in enumerate(self._wrap(d, label, self.f_tiny, cw - int(10 * s), 2)):
                d.text((cx + int(6 * s), y + int(32 * s) + j * int(26 * s)), ln, font=self.f_tiny, fill=FG)
        y += ch + int(14 * s)

        # (g) footer
        self.content_bottom = y
        fy = max(y, self.H - self.FOOTER_H)
        d.line((pad, fy - int(6 * s), W - pad, fy - int(6 * s)), fill=(60, 64, 74), width=1)
        gt = rec.get("gt") or {}
        if gt.get("ok"):
            g = (f"Day {gt.get('day')} {gt.get('clock')}  processed {gt.get('day_processed')}  "
                 f"citations {gt.get('num_citations')}")
            if gt.get("entrant"):
                g += f"  |  {gt.get('entrant')} should be {gt.get('correct')}"
        else:
            g = f"screen {gt.get('screen', '?')}"
        d.text((pad, fy), "Ground truth from game memory — not shown to TOD", font=self.f_small, fill=DIM)
        fy += int(28 * s)
        d.text((pad, fy), self._fit(d, g, self.f_body, W - 2 * pad), font=self.f_body, fill=FG)
        fy += int(34 * s)

        def ms(k):
            v = rec.get(k)
            return f"{v / 1000:.1f}" if isinstance(v, (int, float)) else "-"
        per = ""
        if prev and isinstance(prev.get("time"), (int, float)) and isinstance(rec.get("time"), (int, float)):
            per = f"tick {rec['time'] - prev['time']:.1f}s = "
        lat = (f"{per}vision {ms('extract_ms')} + TOD state {ms('state_ms')} + papers {ms('doc_ms')} "
               f"+ TOD action {ms('tod_ms')} s (+ input, waits)")
        d.text((pad, fy), self._fit(d, lat, self.f_small, W - 2 * pad), font=self.f_small, fill=DIM)
        return img


def render_tick(reader: RunReader, panel: Panel, tick: int, mode: str = "") -> Image.Image | None:
    rec = reader.rec(tick)
    if rec is None:
        return None
    ticks = [t for t in list_ticks(reader.run_dir) if t <= tick][-8:]
    hist = [r for r in (reader.rec(t) for t in ticks) if r is not None]
    if hist and hist[-1].get("tick") == tick:
        hist[-1] = rec
    prev = reader.rec(ticks[-2]) if len(ticks) >= 2 else None
    return panel.render(rec, prev, hist, reader.som(tick), os.path.basename(reader.run_dir.rstrip("\\/")), mode)
