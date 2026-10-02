"""manual.py -- the Papers, Please playing guide TOD reads every tick.

TOD is a single-image vision picker with no memory and no planner. Each tick
it gets two requests (loop.py):

1. STATE, on the unmarked frame: `state_questions(today)` -- simple yes/no
   facts about the picture (person at the window? passport on the counter?
   tray out? ...) plus the inspection decisions (issuing country, expiry,
   photo). Nothing here comes from detector labels; TOD reads the image.
2. ACTION, on the Set-of-Mark frame: `build(state, history, day, bans)` --
   the WHOLE manual below (every situation -> next-step rule, click vs drag,
   stamp alignment, hand-back, page turning, day rules, what each desk element
   does), followed by a "what is currently true on screen" block built from
   request 1's answers, and the last 30 actions. The manual is static; only the
   state block and the history change between ticks. No rule is pre-selected
   for TOD: it matches the situation itself.

`situation(state)` names the manual step that applies to a state. It is for
logs and dry-run evaluation only and is never sent to TOD.

`input_class(box, booth)` is the click-only / drag-only convention the manual
states, used by loop.py to enforce it on TOD's pick.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------
# days (docs/game.md section 5)
# --------------------------------------------------------------------------

DAY_DATES = {"1": "1982.11.23", "2": "1982.11.24", "3": "1982.11.25"}

DAY_RULES = {
    "1": "Day 1 (1982.11.23): only the passport is required. Only citizens of Arstotzka may enter. "
         "APPROVED if the passport's issuing country is ARSTOTZKA and its expiry date is after 1982.11.23; "
         "otherwise DENIED.",
    "2": "Day 2 (1982.11.24): passport only. Foreigners may now enter too. APPROVED if the passport is not "
         "expired (expiry after 1982.11.24) and the photo matches the person; otherwise DENIED.",
    "3": "Day 3 (1982.11.25): Arstotzkan citizens need only a valid passport. Foreigners need a valid passport "
         "AND an entry ticket dated 1982.11.25; a missing ticket or a ticket with any other date -> DENIED.",
}

# --------------------------------------------------------------------------
# the manual (static text, sent in full every tick)
# --------------------------------------------------------------------------

MANUAL = """\
PAPERS, PLEASE -- HOW TO WORK THE BORDER BOOTH WITH THE MOUSE

You are the border inspector. Every input is either a CLICK on one numbered element or a DRAG of one
numbered element onto another numbered element. You decide one input per turn.

1. WHAT IS WHERE ON THE BOOTH SCREEN
- Top strip: the border yard seen from above (the queue of people on the left, guards, the road). Just
  above the booth window, on the booth roof, sits a dark box with a LOUDSPEAKER (horn). Clicking the
  loudspeaker calls the next person in the queue.
- Left side, below the yard: the BOOTH WINDOW. When an entrant is being served you see a person from the
  chest up behind the glass. A yellow SHUTTER LEVER sits at the top-right corner of the window frame.
- Under the window: the grey-green COUNTER SHELF. The entrant puts their documents here, and this is where
  you hand documents back.
- Under the counter shelf: a row of small drawers and readouts (date, rulebook drawer, weight). The date
  readout shows today's date. They are not needed to process an entrant.
- Right of the window, the whole lower right of the screen: your dark dotted DESK. Documents are read here.
  Faint text near its bottom says DRAG DOCUMENTS HERE.
- Right edge of the desk: a small grey TAB sticks out. It is the handle of the STAMP TRAY.
- When the stamp tray is out, a grey bar crosses the upper desk with two big stamps on it: a red DENIED
  stamp (left) and a green APPROVED stamp (right), each with a dark round knob on top (the stamp head).
  Under the bar runs a dark strip with the words ALIGN VISA BENEATH STAMP: that strip is where a passport
  must lie for a stamp to mark it.

2. CLICK OR DRAG -- EACH ELEMENT HAS EXACTLY ONE
CLICK only (never drag these):
- the loudspeaker / horn on the booth roof;
- the APPROVED and DENIED stamps (and their knobs) -- a stamp is pressed by clicking it;
- buttons and menu text (STORY, day tiles, NEXT, CONTINUE, WALK TO WORK ...);
- page corners -- the bottom-right corner of a multi-page paper turns its page;
- text/cutscene screens that have no button (click the text).
DRAG only (clicking them does nothing):
- every document: the closed passport on the counter, the open passport on the desk, papers, the
  bulletin, the rulebook. Press on the document, move it, release it where it should go;
- the stamp tray TAB at the right edge of the desk (drag it LEFT to pull the tray out, RIGHT to put it away);
- the shutter lever.
Dragging a stamp does nothing. Clicking a document does nothing. Clicking empty desk does nothing.

3. DROP TARGETS (marked regions, used only as the end point of a drag)
- "stamp landing strip (under the APPROVED stamp head)" / "(under the DENIED stamp head)": the part of the
  dark strip directly beneath that stamp. Drop the passport here so the stamp lands on it.
- "counter shelf (hand documents back here)": the shelf under the booth window. Dropping a document here
  gives it back to the entrant.
- "desk (drop documents here to read them)": free desk space to the left of the stamp tray. Drop documents
  here to open and read them, or to move a bulletin/rulebook out of the way.

4. PROCESSING ONE ENTRANT -- FIND THE FIRST LINE THAT MATCHES WHAT IS CURRENTLY TRUE
A. Nobody is at the window and no document is on the counter or desk: click the loudspeaker on the booth
   roof to call the next person. The person then walks up to the window by themselves; if someone is
   already walking up, wait.
B. A person is at the window and their passport lies on the counter shelf under the window: drag the
   passport down to the desk ("desk" target) to open it. Clicking it does nothing.
C. An open passport lies on the desk and the stamp tray is closed: read the passport (issuing country at
   the bottom, EXP. date, photo), then open the stamp tray by dragging the tab at the right edge of the
   desk to the left (drop it on the "desk" target).
D. The stamp tray is open but the passport is NOT lying under a stamp head (it is off to the side, or it
   has slid up behind the tray so only an edge shows): stamps only mark a document lying directly beneath
   the stamp heads, in the dark strip under the tray. Drag the passport to the stamp landing strip under
   the stamp you will use (APPROVED or DENIED, see section 5). Do not drag the stamps; they are clicked.
   Do not drop the passport onto the tray bar itself: it slides behind the tray where no stamp reaches it.
E. The stamp tray is open, the passport lies under a stamp head, and it has no stamp mark yet: decide with
   section 5, then click APPROVED or DENIED (one click).
F. The passport shows a stamp mark (green APPROVED or red DENIED ink on its page): drag the stamped passport
   to the counter shelf under the window ("counter shelf" target) to hand it back. The person takes it and
   leaves on their own. Hand back every document the entrant gave you.
G. After the person has left (window empty, nothing on the counter): go back to A and call the next person.
An entrant is finished only after their passport is stamped AND handed back. Clicking the loudspeaker
while someone is still at the window does nothing.

5. DECIDING: APPROVED OR DENIED
Read three things on the open passport: the issuing country (printed in large letters at the bottom of
the passport, e.g. ARSTOTZKA), the EXP. (expiry) date, and the photo compared with the person at the
window.
- Day 1, 1982.11.23: click APPROVED if the issuing country is ARSTOTZKA and the expiry date is after
  1982.11.23; otherwise click DENIED.
- Day 2, 1982.11.24: foreigners may enter too. APPROVED if not expired (expiry after 1982.11.24) and the
  photo matches the person; otherwise DENIED.
- Day 3, 1982.11.25: Arstotzkans need a valid passport only. Foreigners also need an entry ticket dated
  1982.11.25; no ticket or a different date -> DENIED.
The first entrant of day 1 is the tutorial; follow the same rule (his passport is Arstotzkan -> APPROVED).

6. BULLETIN, RULEBOOK AND MULTI-PAGE PAPERS
- The bulletin (Ministry of Admission sheet) and the rulebook can lie open on the desk. If one covers the
  passport or the place you need to work, drag it aside to the left part of the desk ("desk" target). They
  are not needed to process day-1 entrants.
- Multi-page papers (the bulletin shows "3/4" at its bottom) turn pages when you click their bottom-right
  corner. Do not drag a page corner.

7. OTHER SCREENS
- Menus: click STORY, then the day tile, then the button that continues. Never click a trash/delete icon.
- Full-screen text, newspaper or bulletin screens: click the text or the button (NEXT, WALK TO WORK).
- End of day: click the button that continues to the next day.
- While something is moving (the person walking in, a screen fading), choose wait.

8. MISTAKES SEEN BEFORE (do not repeat)
- Dropping the passport on "empty space" under the tray: it hid behind the tray and was never stamped.
- Dragging the APPROVED/DENIED stamps around: nothing happens; stamps are clicked.
- Clicking APPROVED while the passport was above the tray instead of under the stamp: no mark landed.
- Clicking the loudspeaker over and over while the entrant was still at the window.
- Clicking the clock/date drawer: it does nothing useful.
"""

# --------------------------------------------------------------------------
# request 1: state questions (answered from the UNMARKED frame)
# --------------------------------------------------------------------------

STATE_KEYS = ("person_at_window", "document_on_counter_shelf", "document_open_on_desk", "stamp_tray_open",
              "document_under_stamp_heads", "passport_shows_stamp_mark", "bulletin_or_rulebook_covering_desk")
INSPECT_KEYS = ("issuing_country", "expiry_after_today", "photo_matches_person")

STATE_TEXT = ("A screenshot of the game Papers, Please (border inspection booth). Answer each question only "
              "from what is visible in this picture.")


def _noul(instr: str, yes: str, no: str) -> dict:
    return {"type": "noul", "instructions": instr, "criteria": {"true": yes, "false": no}}


def state_questions(today: str = DAY_DATES["1"]) -> dict:
    """The facts the manual is keyed on, asked as one TOD request over the plain
    frame. All noul except issuing_country (choice)."""
    return {
        "person_at_window": _noul(
            "Look at the left third of the picture, behind the window with the height marks 1.1 to 1.9. "
            "Is a person's head and shoulders visible there?",
            "yes - a person's head and shoulders are visible behind that window",
            "no - only the empty dark wall with height marks is visible there"),
        "document_on_counter_shelf": _noul(
            "Look at the grey-green counter shelf directly under the booth window (left side). Does a "
            "passport or other document lie on that shelf?",
            "yes - a passport/document lies on the counter shelf under the window",
            "no - the counter shelf is empty"),
        "document_open_on_desk": _noul(
            "Look at the dark desk on the right half of the picture. Is any part of a passport page (cream "
            "paper with a name, numbers or a photo) visible there, even if only a thin strip sticks out above "
            "or beside the grey stamp bar? The bulletin (dark blue sheet) does not count.",
            "yes - some part of a cream passport page is visible on the right half",
            "no - no passport paper visible on the right half"),
        "stamp_tray_open": _noul(
            "Is the stamp tray pulled out over the desk: a grey bar with a big red DENIED stamp and a big "
            "green APPROVED stamp on it?",
            "yes - the red DENIED and green APPROVED stamps are visible on a bar over the desk",
            "no - no stamps visible (only a small grey tab at the right edge of the desk)"),
        "document_under_stamp_heads": _noul(
            "Look at the dark band directly BELOW the grey bar with the words ALIGN VISA BENEATH STAMP, under "
            "the red and green stamps. Is a cream passport page lying inside that dark band, under the red or "
            "green stamp?",
            "yes - a cream passport page lies in the dark band below the bar, under a stamp",
            "no - that dark band is empty, or there is no stamp bar"),
        "passport_shows_stamp_mark": _noul(
            "Does a passport visible anywhere on screen carry a stamp mark: green APPROVED or red DENIED ink "
            "printed on its page (not the stamps on the tray themselves)?",
            "yes - a green APPROVED or red DENIED ink mark is printed on a passport page",
            "no - no stamp ink on any visible passport"),
        "bulletin_or_rulebook_covering_desk": _noul(
            "Does an open bulletin (Ministry of Admission sheet) or the rulebook lie ON TOP of the passport, "
            "hiding part of it? A bulletin lying next to the passport without covering it does not count.",
            "yes - a bulletin/rulebook covers part of the passport",
            "no - nothing covers the passport (or there is no passport)"),
        "issuing_country": {
            "type": "choice",
            "instructions": "If an open passport is visible: which issuing country is printed on it (large "
                            "letters at the bottom of the passport page)?",
            "criteria": {"ARSTOTZKA": "the passport says ARSTOTZKA",
                         "other": "another country name, or no open passport / not readable"}},
        "expiry_after_today": _noul(
            f"Today is {today}. If an open passport is visible: is its EXP. (expiry) date after today?",
            f"yes - the EXP. date is later than {today}",
            "no - expired, or no readable EXP. date"),
        "photo_matches_person": _noul(
            "If an open passport is visible: does the photo on the passport show the same person who stands "
            "at the booth window (face, hair/hood, glasses)?",
            "yes - the photo matches the person at the window",
            "no - the photo shows someone else, or there is no photo/person to compare"),
    }


# --------------------------------------------------------------------------
# request 2 text
# --------------------------------------------------------------------------

_LABEL = {
    "person_at_window": "A person is at the booth window",
    "document_on_counter_shelf": "A document lies on the counter shelf under the window",
    "document_open_on_desk": "An open passport lies on the desk",
    "stamp_tray_open": "The stamp tray is pulled out (APPROVED/DENIED stamps visible)",
    "document_under_stamp_heads": "A passport lies under a stamp head (in the strip under the tray)",
    "passport_shows_stamp_mark": "A passport shows a stamp mark",
    "bulletin_or_rulebook_covering_desk": "A bulletin/rulebook covers the passport",
}


def yes(state: dict, k: str, thr: float = 0.5) -> bool:
    v = state.get(k)
    return bool(v) and v.get("p", 0.0) >= thr


def _yn(state: dict, k: str) -> str:
    v = state.get(k)
    if not v:
        return "unknown"
    p = v["p"]  # P(yes); shown as the confidence of the stated answer
    return f"yes (p={p:.2f})" if p >= 0.5 else f"no (p={1 - p:.2f})"


def state_block(state: dict, day: str) -> str:
    today = DAY_DATES.get(day, DAY_DATES["1"])
    lines = ["WHAT IS CURRENTLY TRUE ON SCREEN (read from this frame by a separate check; p = confidence):"]
    scr = state.get("screen")
    if scr:
        lines.append(f"- Screen: {scr['value']} (p={scr['p']:.2f})")
    for k in STATE_KEYS:
        lines.append(f"- {_LABEL[k]}: {_yn(state, k)}")
    if yes(state, "document_open_on_desk"):
        c = state.get("issuing_country")
        if c:
            lines.append(f"- Passport issuing country: {c['value']} (p={c['p']:.2f})")
        lines.append(f"- Passport expiry date is after today ({today}): {_yn(state, 'expiry_after_today')}")
        lines.append(f"- Passport photo matches the person at the window: {_yn(state, 'photo_matches_person')}")
    d = DAY_RULES.get(day)
    lines.append(f"- Day: {day} -- {d}" if d else "- Day: not yet known (treat as day 1 until a later date shows)")
    return "\n".join(lines)


def state_summary(state: dict) -> str:
    """Compact one-line state for the history block, e.g. 'person, passport on desk, tray open'."""
    if not state:
        return "state unknown"
    scr = state.get("screen", {}).get("value", "")
    if scr and scr not in ("booth_idle", "documents_on_desk", "stamp_tray_open", "inspect_mode"):
        return scr
    bits = []
    bits.append("person" if yes(state, "person_at_window") else "no person")
    if yes(state, "document_on_counter_shelf"):
        bits.append("doc on counter")
    if yes(state, "document_open_on_desk"):
        bits.append("passport on desk")
    bits.append("tray open" if yes(state, "stamp_tray_open") else "tray closed")
    if yes(state, "document_under_stamp_heads"):
        bits.append("passport under stamp")
    if yes(state, "passport_shows_stamp_mark"):
        bits.append("STAMPED")
    if yes(state, "bulletin_or_rulebook_covering_desk"):
        bits.append("covered")
    return ", ".join(bits)


def build(state: dict, history, day: str, ban_lines: list[str] | None = None) -> str:
    """Request-2 text: the whole manual + what is true now + last actions."""
    hist = list(history)
    h = "\n".join(f"- {x}" for x in hist) if hist else "- (none yet; this is the first action)"
    head = f"LAST {len(hist)} ACTIONS" if hist else "LAST ACTIONS"
    parts = [MANUAL, state_block(state, day),
             f"{head} (oldest first; tick | what was true | input | element | effect):\n{h}"]
    if ban_lines:
        parts.append("RULED OUT FOR NOW (tried without effect):\n" + "\n".join(f"- {s}" for s in ban_lines))
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# diagnostics only (never sent to TOD)
# --------------------------------------------------------------------------


def situation(state: dict, day: str = "1") -> tuple[str, str]:
    """(step letter, intended input) per manual section 4 -- for logs and the
    dry-run pass/fail check only."""
    scr = state.get("screen", {}).get("value", "")
    if scr and scr not in ("booth_idle", "documents_on_desk", "stamp_tray_open", "inspect_mode"):
        return "7", f"non-booth screen ({scr}): click to continue"
    if yes(state, "passport_shows_stamp_mark"):
        return "F", "drag stamped passport -> counter shelf"
    if yes(state, "bulletin_or_rulebook_covering_desk") and yes(state, "document_open_on_desk"):
        return "6", "drag bulletin/rulebook -> desk (aside)"
    if yes(state, "stamp_tray_open"):
        if yes(state, "document_under_stamp_heads"):
            ok = yes(state, "expiry_after_today") and state.get("issuing_country", {}).get("value") == "ARSTOTZKA"
            if day == "2":
                ok = yes(state, "expiry_after_today") and yes(state, "photo_matches_person")
            return "E", "click " + ("APPROVED" if ok else "DENIED")
        if yes(state, "document_open_on_desk"):
            return "D", "drag passport -> stamp landing strip"
    if yes(state, "document_on_counter_shelf") and not yes(state, "document_open_on_desk"):
        return "B", "drag passport (counter) -> desk"
    if yes(state, "document_open_on_desk") and not yes(state, "stamp_tray_open"):
        return "C", "drag tray tab -> left (open tray)"
    if not yes(state, "person_at_window"):
        return "A", "click loudspeaker"
    return "?", "person at window, nothing actionable detected: wait"


# --------------------------------------------------------------------------
# click-only / drag-only convention (manual section 2)
# --------------------------------------------------------------------------

_STAMP_RE = re.compile(r"\b(APPRO\w*|DENI\w*)\b", re.I)
CLICK_CAPS = {"speaker/horn", "rubber stamp", "red rubber stamp", "green rubber stamp", "button"}
DRAG_CAPS = {"closed passport", "open passport", "rulebook / ring binder", "document on counter",
             "passport booklet", "paper document", "ticket", "bulletin board", "tab at screen edge",
             "lever handle"}


def input_class(box, booth: bool) -> str | None:
    """'click' (horn, stamps, buttons, page corners), 'drag' (documents, tray tab,
    lever) or None (unconstrained). Regions are drop targets only ('target')."""
    kind = getattr(box, "kind", "")
    cap = getattr(box, "caption", "") or ""
    text = getattr(box, "text", "") or ""
    if kind == "region":
        return "target"
    if kind == "page_corner":
        return "click"
    if cap in CLICK_CAPS:
        return "click"
    if text and _STAMP_RE.search(text) and len(text) <= 12:
        return "click"
    if cap in DRAG_CAPS:
        return "drag"
    if booth and kind == "panel" and text and len(text) > 12:
        return "drag"   # a texted sheet on the desk is a document
    return None
