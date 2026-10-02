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

# --------------------------------------------------------------------------
# days (docs/game.md section 5)
# --------------------------------------------------------------------------

DAY_DATES = {"1": "1982.11.23", "2": "1982.11.24", "3": "1982.11.25"}

DAY_RULES = {
    "1": "Day 1 (1982.11.23): only the passport is required. The ONLY rule today: APPROVED if the passport's "
         "issuing country is ARSTOTZKA, otherwise DENIED. Expiry date and photo are NOT checked on Day 1 "
         "(those checks start on Day 2).",
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
- Under the window: the grey-green COUNTER SHELF. The entrant puts their documents here. To hand documents
  back, drop them on the PERSON in the window, not on the shelf.
- Under the counter shelf: a row of small drawers and readouts (date, rulebook drawer, weight). The date
  readout shows today's date. They are not needed to process an entrant.
- Right of the window, the whole lower right of the screen: your dark dotted DESK. Documents are read here.
  Faint text near its bottom says DRAG DOCUMENTS HERE.
- Right edge of the desk: a small grey TAB sticks out. It is the handle of the STAMP TRAY. When the tray
  is out, its tab is at the LEFT end of the grey stamp bar ("stamp tray tab (left end of the open stamp
  bar)").
- When the stamp tray is out, a grey bar crosses the upper desk with two big stamps on it: a red DENIED
  stamp (left) and a green APPROVED stamp (right), each with a dark round knob on top (the stamp head).
  Each stamp is ONE element: clicking anywhere on it (knob or red/green body) presses it.
  Under the bar runs a dark strip with the words ALIGN VISA BENEATH STAMP: that strip is where a passport
  must lie for a stamp to mark it. A stamp only marks what lies in the strip directly under THAT stamp.

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
- "the entrant at the booth window -- drop documents ON THE PERSON to hand them back": the person standing
  in the booth window. Dropping a document on the person gives it back; they say "Thank you." and leave.
  Offered whenever a person is at the window. Dropping a document on the counter shelf under the window
  does NOT hand it back -- it just lies there.
- "desk (drop documents here to read them)": free desk space to the left of the stamp tray. Drop documents
  here to open and read them, or to move a bulletin/rulebook out of the way.
- "right edge of the desk (drag the tray tab here to put the stamp tray away)": offered while the tray is
  out. Dragging the tray tab here closes the tray.

4. PROCESSING ONE ENTRANT -- FIND THE FIRST LINE THAT MATCHES WHAT IS CURRENTLY TRUE
A. Nobody is at the window and no document is on the counter or desk: click the loudspeaker on the booth
   roof to call the next person. The loudspeaker ONLY works when the window is empty and the desk is clear;
   while a person stands at the window it does nothing (it is then not even offered). The person then walks up to the window by themselves; if someone is
   already walking up, wait.
B. A person is at the window and their passport lies on the counter shelf under the window: drag the
   passport down to the desk ("desk" target) to open it. Clicking it does nothing.
C. An open passport lies on the desk and the stamp tray is closed: read the passport (on Day 1 only the
   issuing country at the bottom matters; from Day 2 also the EXP. date and photo), then open the stamp tray by dragging the tab at the right edge of the
   desk to the left (drop it on the "desk" target).
D. The stamp tray is open but the passport is NOT lying under a stamp head (it is off to the side, or it
   has slid up behind the tray so only an edge shows): stamps only mark a document lying directly beneath
   the stamp heads, in the dark strip under the tray. First decide APPROVED or DENIED (section 5), then
   drag the passport to the stamp landing strip under THAT stamp. Both strips are valid landing places:
   the APPROVED strip and the DENIED strip each work; put the passport under the stamp you intend to use.
   Do not drag the stamps; they are clicked. Do not drop the passport onto the tray bar itself: it slides
   behind the tray where no stamp reaches it.
D2. RECOVERY: the entrant's passport is no longer visible anywhere on the desk or the counter shelf (it
   slid behind the open stamp tray, so the desk looks empty while the person is still at the window). If
   the passport is on the counter shelf, that is rule B, not D2: leave the tray open and drag the passport
   to the desk. Only for a hidden passport: close the stamp tray by
   dragging its tab (left end of the open stamp bar) back to the RIGHT (drop it on the "right edge of the
   desk" target). The hidden passport
   reappears; then continue with C.
E0. A paper that is NOT the passport (the rulebook, the bulletin, a transcript) lies under a stamp (the state
   block says "the RULEBOOK ... lies under the DENIED stamp, not the passport"): stamping it is useless and
   will be refused. Drag that paper off the strip to the "desk" target, then drag the PASSPORT (from the
   counter shelf or the desk) to the strip under the stamp you need. The rulebook is never needed on Day 1.
E. The stamp tray is open, the passport lies under a stamp head (the state block says "The passport is
   under: APPROVED" or "DENIED"), and it is NOT stamped yet (none of the three "stamped" signs of F is in the
   state block): decide with section 5, then click the stamp the passport is lying under; if you want the
   other decision, first drag the passport to the other strip. ONE click. Clicking the stamp the passport is
   NOT under stamps nothing. Decide only when the issuing country is known -- the state block shows it either as read in this
   frame or as "passport read as <COUNTRY> at tick N". If the country is not known yet (the bottom of the
   passport with the country name is not visible), do not stamp: drag the passport to the "desk" target so
   the whole page can be read, then put it under the stamp you need.
F. The passport IS STAMPED. Any ONE of these lines in the state block is enough:
   (1) "A passport shows a stamp mark: yes";
   (2) "A stamp was clicked at tick N and the screen changed: the passport is stamped".
   Then STOP clicking stamps: drag the stamped passport onto the person at the window ("the entrant at the
   booth window -- drop documents ON THE PERSON to hand them back") to hand it back. The person takes it and
   leaves on their own. Hand back every document the entrant gave you. The open stamp tray does not have to
   be closed first; drag the passport by the part that is visible.
F2. WRONG STAMP: the state block says which stamp was clicked. If the passport was stamped APPROVED but the
   rule (section 5) says DENIED, click DENIED once more -- a DENIED stamp overrules APPROVED -- then hand it
   back. If it was stamped DENIED but should have been APPROVED, it cannot be fixed (DENIED always wins and
   an APPROVED stamp on top does not count): hand it back as it is. The first two mistakes of each day are
   only warnings.
G. After the documents were handed back (the state block says so) the person leaves by themselves; wait
   while they walk away. When the window is empty and nothing is on the counter: go back to A and click the
   loudspeaker to call the next person.
An entrant is finished only after their passport is stamped AND handed back. Clicking the loudspeaker
while someone is still at the window does nothing.

5. DECIDING: APPROVED OR DENIED
- Day 1, 1982.11.23: the ONLY rule is the issuing country (printed in large letters at the bottom of the
  passport, e.g. ARSTOTZKA). Issuing country ARSTOTZKA -> APPROVED. Any other country -> DENIED.
  Expiry date and photo are NOT Day 1 rules; do not deny anyone on Day 1 for expiry or photo.
- Day 2, 1982.11.24 (expiry and photo checks start today): foreigners may enter too. APPROVED if not
  expired (expiry after 1982.11.24) and the photo matches the person; otherwise DENIED.
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
- Main menu (title screen): click STORY.
- Day-select screen ("Select day to continue or start a new game"): a row of day tiles near the top left.
  At the start the only tile is DAY 1 / NEW -- click it (it is a tile, not a button). Never click BACK,
  QUIT, or the trash/delete icon on this screen: BACK returns to the main menu and undoes progress, the
  trash icon deletes the save. If no day tile is drawn yet, choose wait.
- After the day tile, the intro, newspaper and bulletin screens advance with NEXT, then WALK TO WORK takes
  you to the booth. Full-screen text without a button: click the text.
- End of day: click the button that continues to the next day.
- While something is moving (the person walking in, a screen fading), choose wait.

8. MISTAKES SEEN BEFORE (do not repeat)
- Dropping the passport on "empty space" under the tray: it hid behind the tray and was never stamped.
- Dragging the APPROVED/DENIED stamps around: nothing happens; stamps are clicked.
- Clicking APPROVED while the passport was above the tray instead of under the stamp: no mark landed.
- Clicking the loudspeaker over and over while the entrant was still at the window.
- Clicking the clock/date drawer: it does nothing useful.
- Run 20261002_083908: the passport was stamped DENIED at tick 9, then the stamps were clicked 10 more
  times instead of handing it back. Once ANY of the three stamped signs is shown, hand the passport back.
- Run 20261002_083908: the stamp was chosen while the issuing country was not yet visible (it read "other"
  with only the visa page in view). Stamp only once the country name has been read.
- Leaving the passport hidden behind the open tray and clicking the loudspeaker: nothing happens; close
  the tray (drag its tab right) to get the passport back (rule D2).
- Run 20261002_092612: the stamped passport was dropped on the counter shelf 5 times; it just lay there and
  the entrant never took it. Drop it ON THE PERSON in the window instead.
- Clicking the stamp the passport is NOT under: the stamp comes down on the empty strip and nothing is
  marked. Click the stamp the passport is lying under, or drag the passport to the other strip first.
"""

# --------------------------------------------------------------------------
# request 1: state questions (answered from the UNMARKED frame)
# --------------------------------------------------------------------------

STATE_KEYS = ("person_at_window", "document_on_counter_shelf", "document_open_on_desk", "passport_open_readable",
              "stamp_tray_open", "passport_shows_stamp_mark", "bulletin_or_rulebook_covering_desk")
STRIP_KEYS = ("passport_under_denied", "passport_under_approved")
COUNTRIES = ("ARSTOTZKA", "KOLECHIA", "IMPOR", "ANTEGRIA", "OBRISTAN", "REPUBLIA", "UNITED FEDERATION")
DOC_KINDS = {"passport": "the entrant's passport (a small booklet or its open data page: photo, name, DOB, SEX, ISS., "
                         "EXP., the issuing country name in large letters)",
             "rulebook": "the inspector's rulebook (blue-grey cover 'RULES & REGULATIONS', or open pages: CONTENTS, "
                         "Basic Rules, Regional Map, Booth Info)",
             "bulletin": "the Ministry of Admission bulletin (a sheet with today's rules / news)",
             "entry_ticket": "an entry ticket (small slip with a date)",
             "transcript": "the interview transcript printout",
             "other": "something else, or not a paper"}
INSPECT_KEYS = ("issuing_country", "expiry_after_today", "photo_matches_person")

STATE_TEXT = ("A screenshot of the game Papers, Please (border inspection booth). Answer each question only "
              "from what is visible in this picture.")


def _noul(instr: str, yes: str, no: str) -> dict:
    return {"type": "noul", "instructions": instr, "criteria": {"true": yes, "false": no}}


def state_questions(today: str = DAY_DATES["1"], inspect: tuple = INSPECT_KEYS) -> dict:
    """The facts the manual is keyed on, asked as one TOD request over the plain
    frame. All noul except issuing_country (choice). `inspect` selects which
    inspection questions are asked (loop.py gates them on the desk OCR)."""
    q = {
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
        "passport_open_readable": _noul(
            "Is the entrant's PASSPORT lying open on the dark desk (right half) with its data page readable: photo, "
            "name, dates and the issuing country? The rulebook, the bulletin or a closed booklet do not count.",
            "yes - an open passport data page is readable on the desk",
            "no - no open, readable passport on the desk"),
        "passport_under_denied": _noul(
            "Look at the dark band directly BELOW the red DENIED stamp (under the grey bar with the words ALIGN "
            "VISA BENEATH STAMP). Is the paper lying in that band, under the DENIED stamp, the entrant's PASSPORT? "
            "The rulebook (pages with CONTENTS / Basic Rules / Regional Map), the bulletin or any other paper is NOT "
            "a passport.",
            "yes - the entrant's passport lies under the DENIED stamp",
            "no - nothing, or a paper that is not the passport (rulebook, bulletin ...), lies under the DENIED stamp, "
            "or there is no stamp bar"),
        "passport_under_approved": _noul(
            "Look at the dark band directly BELOW the green APPROVED stamp (under the grey bar with the words ALIGN "
            "VISA BENEATH STAMP). Is the paper lying in that band, under the APPROVED stamp, the entrant's PASSPORT? "
            "The rulebook (pages with CONTENTS / Basic Rules / Regional Map), the bulletin or any other paper is NOT "
            "a passport.",
            "yes - the entrant's passport lies under the APPROVED stamp",
            "no - nothing, or a paper that is not the passport (rulebook, bulletin ...), lies under the APPROVED "
            "stamp, or there is no stamp bar"),
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
            "instructions": "Issuing country of the open passport: the country name printed in large letters on "
                            "the passport (bottom of the data page / cover). The rulebook and bulletin mention "
                            "'Arstotzkan' too -- only the PASSPORT counts.",
            "criteria": {**{c: f"the passport is issued by {c}" for c in COUNTRIES},
                         "unreadable": "no open passport, or its country name cannot be read"}},
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
    return {k: v for k, v in q.items() if k not in INSPECT_KEYS or k in inspect}


# --------------------------------------------------------------------------
# request 2 text
# --------------------------------------------------------------------------

_LABEL = {
    "person_at_window": "A person is at the booth window",
    "document_on_counter_shelf": "A document lies on the counter shelf under the window",
    "document_open_on_desk": "An open passport lies on the desk",
    "stamp_tray_open": "The stamp tray is pulled out (APPROVED/DENIED stamps visible)",
    "passport_open_readable": "An open passport data page is readable on the desk",
    "passport_shows_stamp_mark": "A passport shows a stamp mark",
    "bulletin_or_rulebook_covering_desk": "A bulletin/rulebook covers the passport",
}


def doc_question(d: dict) -> dict:
    """Request-1 identity question for one paper the layout found (position + the OCR text inside it)."""
    where = "on the counter shelf under the booth window" if d["where"] == "counter" else "on the dark desk"
    txt = "; ".join(repr(t) for t in d.get("text") or []) or "(no readable text)"
    return {"type": "choice",
            "instructions": f"Look at the paper lying {where}, at the {d['pos']} of the picture. OCR read inside "
                            f"it: {txt}. What is this paper?",
            "criteria": dict(DOC_KINDS)}


def under_phrase(facts: dict, side: str) -> str:
    """'the RULEBOOK (p=0.93) lies under the DENIED stamp, not the passport' etc. (from TOD's answers)."""
    v = (facts.get("strip") or {}).get(side) or {}
    S = side.upper()
    if v.get("paper") is False:
        return f"nothing lies under the {S} stamp (the strip is empty)"
    if v.get("doc") and v["doc"] != "passport":
        return f"the {v['doc'].upper()} (p={v['doc_p']:.2f}) lies under the {S} stamp, not the passport"
    pp = v.get("passport_p")
    if pp is not None and pp < 0.5:
        return f"the paper under the {S} stamp is not the passport (p={1 - pp:.2f})"
    return f"no passport under the {S} stamp"


def yes(state: dict, k: str, thr: float = 0.5) -> bool:
    v = state.get(k)
    return bool(v) and v.get("p", 0.0) >= thr


def _yn(state: dict, k: str) -> str:
    v = state.get(k)
    if not v:
        return "unknown"
    p = v["p"]  # P(yes); shown as the confidence of the stated answer
    return f"yes (p={p:.2f})" if p >= 0.5 else f"no (p={1 - p:.2f})"


def desk_text_block(facts: dict | None) -> str:
    """'READABLE TEXT ON THE DESK' -- the OCR'd text of every document on the desk
    (extraction output, sent to both requests)."""
    facts = facts or {}
    lines = facts.get("desk_text") or []
    if not lines:
        return "READABLE TEXT ON THE DESK (OCR of the documents on the desk): none"
    out = ["READABLE TEXT ON THE DESK (OCR of the documents on the desk, top to bottom; pixel font, may "
           "contain misreads):"]
    out += [f"- {t}" for t in lines]
    return "\n".join(out)


def stamped(state: dict, facts: dict | None) -> list[str]:
    """The three 'passport is stamped' signs that are currently true (manual rule F)."""
    facts = facts or {}
    out = []
    if yes(state, "passport_shows_stamp_mark"):
        out.append("mark")
    if facts.get("stamp_clicks"):
        out.append("history")
    return out


def known_country(state: dict, facts: dict | None):
    """(value, p, where) of the issuing country: this frame's reading if it was asked, else the most recent
    confident reading carried from earlier ticks of this entrant, else None."""
    c = state.get("issuing_country")
    if c:
        return c["value"], c["p"], "this frame"
    cc = (facts or {}).get("country_carried")
    if cc:
        return cc["value"], cc["p"], f"tick {cc['tick']}"
    return None


def state_block(state: dict, day: str, facts: dict | None = None) -> str:
    facts = facts or {}
    today = DAY_DATES.get(day, DAY_DATES["1"])
    lines = ["WHAT IS CURRENTLY TRUE ON SCREEN (read from this frame by a separate check; p = confidence):"]
    scr = state.get("screen")
    if scr:
        lines.append(f"- Screen: {scr['value']} (p={scr['p']:.2f})")
    for k in STATE_KEYS:
        lines.append(f"- {_LABEL[k]}: {_yn(state, k)}")
    for d in facts.get("docs_named") or []:
        where = "counter shelf" if d["where"] == "counter" else "desk"
        lines.append(f"- Paper on the {where} ({d['pos']}): {d['id'].upper()} (p={d['p']:.2f})")
    if yes(state, "passport_open_readable") and not yes(state, "stamp_tray_open"):
        lines.append("- The passport is already open on the desk and the stamp tray is closed: dragging the passport "
                     "to the desk again changes nothing; the next step is C (drag the stamp tray tab to the desk)")
    for t, side in facts.get("stamp_clicks") or []:
        lines.append(f"- A stamp was clicked at tick {t} ({side.upper()}) and the screen changed: the passport is "
                     f"stamped {side.upper()}")
    for t, side in facts.get("missed_stamps") or []:
        lines.append(f"- The {side.upper()} stamp was clicked at tick {t} while the passport lay under the other "
                     "stamp: nothing was stamped")
    if (facts.get("tray_flips") or 0) >= 4:
        lines.append(f"- LOOP WARNING: the stamp tray was opened and closed {facts['tray_flips']} times in the last "
                     "8 ticks without a stamp. Toggling it again achieves nothing: leave the tray as it is and move a "
                     "DOCUMENT instead (the passport onto a stamp landing strip, or a visa/other paper back to the desk)")
    if "strip" in facts and (yes(state, "stamp_tray_open") or facts.get("tray_open_px")):
        pu = facts.get("passport_under") or []
        for side in ("denied", "approved"):
            lines.append(f"- Under the {side.upper()} stamp: " + (
                f"the PASSPORT (p={facts['strip'][side]['passport_p']:.2f})" if side in pu
                else under_phrase(facts, side)))
        lines.append("- The passport is under: " + (" and ".join(x.upper() for x in pu) if pu else "none"))
        wrong = [s_ for s_ in ("denied", "approved") if (facts["strip"][s_].get("doc") or "passport") != "passport"
                 and s_ not in pu]
        if wrong:
            lines.append(f"- A paper that is NOT the passport lies under the {' and '.join(w.upper() for w in wrong)} "
                         "stamp: stamping it is useless (the stamp press will be refused). Drag that paper off the "
                         "strip to the 'desk' target, then drag the PASSPORT to the strip under the stamp you need")
        if len(pu) == 1:
            other = "approved" if pu[0] == "denied" else "denied"
            lines.append(f"- Click the stamp the passport is lying under ({pu[0].upper()}); if you want "
                         f"{other.upper()} instead, first drag the passport to the strip under the {other.upper()} "
                         f"stamp. Clicking {other.upper()} now would stamp nothing.")
        elif not pu:
            lines.append("- The passport lies under neither stamp: clicking a stamp now stamps nothing (it will be "
                         "refused); drag the passport to the landing strip under the stamp you want first")
    if facts.get("handed_back") is not None:
        lines.append(f"- Documents were handed back at tick {facts['handed_back']}: this entrant is finished and "
                     "leaves by themselves; call the next person once the window is empty")
    open_ok = yes(state, "passport_open_readable")
    c = state.get("issuing_country")
    if c:
        lines.append(f"- Passport issuing country (read in this frame): {c['value']} (p={c['p']:.2f})")
    elif open_ok:
        lines.append("- Passport issuing country: not readable in this frame")
    cc = facts.get("country_carried")
    if cc and cc.get("tick") != facts.get("tick"):
        lines.append(f"- Passport read as {cc['value']} at tick {cc['tick']} (p={cc['p']:.2f})")
    if open_ok and day in ("2", "3"):
        if "expiry_after_today" in state:
            lines.append(f"- Passport expiry date is after today ({today}): {_yn(state, 'expiry_after_today')}")
        if "photo_matches_person" in state:
            lines.append(f"- Passport photo matches the person at the window: {_yn(state, 'photo_matches_person')}")
    elif open_ok:  # Day 1 (or not yet known): expiry/photo are asked and logged but are not Day 1 rules
        lines.append("- (Day 1: expiry and photo are not checked; only the issuing country decides)")
    d = DAY_RULES.get(day)
    lines.append(f"- Day: {day} -- {d}" if d else "- Day: not yet known (treat as day 1 until a later date shows)")
    return "\n".join(lines)


def state_summary(state: dict, facts: dict | None = None) -> str:
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
    if (facts or {}).get("passport_under"):
        bits.append("passport under " + "+".join((facts or {})["passport_under"]))
    st = stamped(state, facts)
    if st:
        bits.append("STAMPED(" + "+".join(st) + ")")
    if (facts or {}).get("handed_back") is not None:
        bits.append("handed back")
    if yes(state, "bulletin_or_rulebook_covering_desk"):
        bits.append("covered")
    return ", ".join(bits)


def entrant_line(facts: dict | None) -> str | None:
    """One line for the history block: what is known about the current entrant so far."""
    facts = facts or {}
    bits = []
    cc = facts.get("country_carried")
    if cc:
        bits.append(f"passport read as {cc['value']} at tick {cc['tick']} (p={cc['p']:.2f})")
    for t, side in facts.get("stamp_clicks") or []:
        bits.append(f"stamped {side.upper()} at tick {t}")
    if facts.get("handed_back") is not None:
        bits.append(f"handed back at tick {facts['handed_back']}")
    return "THIS ENTRANT SO FAR: " + "; ".join(bits) if bits else None


def build(state: dict, history, day: str, ban_lines: list[str] | None = None, facts: dict | None = None) -> str:
    """Request-2 text: the whole manual + what is true now + desk text + last actions."""
    hist = list(history)
    h = "\n".join(f"- {x}" for x in hist) if hist else "- (none yet; this is the first action)"
    el = entrant_line(facts)
    if el:
        h = f"{el}\n{h}"
    head = f"LAST {len(hist)} ACTIONS" if hist else "LAST ACTIONS"
    nb = (facts or {}).get("menu_bounces") or 0
    if nb >= 2:
        h = (f"You have gone back and forth between the main menu and day select {nb} times. BACK undoes "
             f"progress; pick a day tile.\n{h}")
    parts = [MANUAL, state_block(state, day, facts), desk_text_block(facts),
             f"{head} (oldest first; tick | what was true | input | element | effect):\n{h}"]
    if ban_lines:
        parts.append("RULED OUT FOR NOW (tried without effect):\n" + "\n".join(f"- {s}" for s in ban_lines))
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# diagnostics only (never sent to TOD)
# --------------------------------------------------------------------------


def situation(state: dict, day: str = "1", facts: dict | None = None) -> tuple[str, str]:
    """(step letter, intended input) per manual section 4 -- for logs and the
    dry-run pass/fail check only."""
    scr = state.get("screen", {}).get("value", "")
    if scr and scr not in ("booth_idle", "documents_on_desk", "stamp_tray_open", "inspect_mode"):
        return "7", f"non-booth screen ({scr}): click to continue"
    if (facts or {}).get("handed_back") is not None:
        return ("G", "wait for the entrant to leave") if yes(state, "person_at_window") else ("A", "click loudspeaker")
    kc = known_country(state, facts)
    if kc and kc[0] == "unreadable":
        kc = None
    ok = bool(kc) and kc[0] == "ARSTOTZKA"   # Day 1: the only rule
    if day == "2":
        ok = yes(state, "expiry_after_today") and yes(state, "photo_matches_person")
    if stamped(state, facts):
        sides = [s for _, s in (facts or {}).get("stamp_clicks") or []]
        if sides and sides[-1] == "approved" and kc and not ok:
            return "F2", "click DENIED (overrules APPROVED)"
        return "F", "drag stamped passport -> entrant (hand back)"
    if yes(state, "bulletin_or_rulebook_covering_desk") and yes(state, "document_open_on_desk"):
        return "6", "drag bulletin/rulebook -> desk (aside)"
    f = facts or {}
    if yes(state, "stamp_tray_open") or f.get("tray_open_px"):
        wrong = [s_ for s_, v in (f.get("strip") or {}).items() if v.get("doc") and v["doc"] != "passport"]
        if wrong and not f.get("passport_under"):
            return "E0", f"drag the {f['strip'][wrong[0]]['doc']} off the {wrong[0].upper()} strip -> desk"
        if f.get("passport_under"):
            if not kc:
                return "E?", "country unknown: drag passport -> desk to read it"
            need = "approved" if ok else "denied"
            pu = (facts or {}).get("passport_under") or []
            if pu and need not in pu:
                return "E-", f"drag passport -> strip under the {need.upper()} head (it lies under {pu[0].upper()})"
            return "E", "click " + need.upper()
        if yes(state, "document_open_on_desk"):
            return "D", "drag passport -> stamp landing strip"
        if (yes(state, "person_at_window") and (facts or {}).get("tray_flips", 0) < 4
                and not yes(state, "document_on_counter_shelf")):
            return "D2", "drag tray tab -> right edge (close tray, reveal hidden passport)"
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

CLICK_CAPS = {"speaker/horn", "rubber stamp", "red rubber stamp", "green rubber stamp", "button"}
DRAG_CAPS = {"closed passport", "open passport", "rulebook / ring binder", "document on counter",
             "passport booklet", "paper document", "ticket", "bulletin board", "tab at screen edge",
             "stamp tray tab (left end of the open stamp bar)",
             "lever handle"}


def input_class(box, booth: bool) -> str | None:
    """'click' (horn, stamps, buttons, page corners), 'drag' (documents, tray tab,
    lever) or None (unconstrained). Regions are drop targets only ('target')."""
    kind = getattr(box, "kind", "")
    aff = getattr(box, "affordance", "")
    if aff in ("click", "drag"):
        return aff   # layout.py element (static / hybrid extractor)
    if aff == "target":
        return "target"
    cap = getattr(box, "caption", "") or ""
    text = getattr(box, "text", "") or ""
    if kind == "region":
        return "target"
    if kind == "page_corner":
        return "click"
    if cap in CLICK_CAPS:
        return "click"
    if cap in DRAG_CAPS:
        return "drag"
    return None
