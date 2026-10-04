"""manual.py -- the Papers, Please playing guide TOD reads every tick.

TOD is a single-image vision picker with no memory and no planner. Each tick
it gets two requests (loop.py):

1. STATE, on the unmarked frame: `state_questions(today)` -- simple yes/no
   facts about the picture (person at the window? passport on the counter?
   tray out? ...) plus the inspection decisions (issuing country, expiry,
   photo). Nothing here comes from detector labels; TOD reads the image.
2. ACTION, on the Set-of-Mark frame: `build(state, history, day, bans)` --
   the static RULES (booth layout, input convention, today's admission rule,
   the per-entrant goal, time wasters; ~2.4k chars, loop22), then the "what is
   currently true on screen" block from request 1's answers, a "what applies
   now" block (`now_block`: 1-4 plain sentences for the one situation that
   matches, worded from TOD's own answers, no step letters), the desk OCR and
   the last actions.

The verdict (APPROVED / DENIED / cannot_decide_yet) is TOD's own answer to
`verdict_question` (request 1b): today's rule as text plus TOD's own earlier
readings of the entrant's papers, labelled as such. No code computes a verdict
(the old needed_stamp / wrong_stamp / undecided_stamp were removed, audit A1-A4).

`situation(state)` names the step that applies to a state (logs, cycle
signature); its letter is never sent to TOD, only now_block's sentence.

`input_class(box, booth)` is the click-only / drag-only convention the manual
states. loop.py only logs a mismatch with TOD's `action` answer; it never
changes TOD's input.
"""
from __future__ import annotations

import difflib
import re

# --------------------------------------------------------------------------
# days (docs/game.md section 5)
# --------------------------------------------------------------------------

TRAY_FLIP_LIMIT = 3   # open<->closed toggles without a stamp before the loop warns / skips the tray tab
                      # (C open + D2 close + reopen = 2 is legitimate; run 005956 t12-16 ping-ponged 4x)

DAY_DATES = {"1": "1982.11.23", "2": "1982.11.24", "3": "1982.11.25"}

DAY_RULES = {
    "1": "Day 1 (1982.11.23): only the passport is required. The ONLY rule today: APPROVED if the passport's "
         "issuing country is ARSTOTZKA, otherwise DENIED. Expiry date is NOT checked on Day 1 "
         "(that check starts on Day 2).",
    "2": "Day 2 (1982.11.24): passport only. Foreigners may now enter too. APPROVED if the passport is not "
         "expired (expiry after 1982.11.24) and the ISS. city belongs to the passport's "
         "country; otherwise DENIED.",
    "3": "Day 3 (1982.11.25): Arstotzkan citizens need only a valid passport. Foreigners need a valid passport "
         "AND an entry ticket dated 1982.11.25; a missing ticket or a ticket with any other date -> DENIED.",
}

# --------------------------------------------------------------------------
# the manual (static text, sent in full every tick)
# --------------------------------------------------------------------------

# the one input-convention line: manual section 2 and the request-2 `action` question say it identically (B26)
INPUT_LINE = "Click for stamps/buttons/horn/page corners, drag for papers and the tray tab."

# the booth manual: RULES only (loop22 rewrite: the old 17k-char section 4 was a prose script of ~25 lettered steps
# and pushed request 2 into HTTP 413). What applies NOW is stated per tick by now_block() from TOD's own answers.
BOOTH_MANUAL = """\
PAPERS, PLEASE -- BORDER BOOTH RULES
You are the border inspector. Each turn: CLICK one numbered element, DRAG one numbered element onto a numbered
drop target, or WAIT (while something moves, or when nothing should be done).

1. WHAT IS WHERE
- Top strip: the border yard. The LOUDSPEAKER (horn) on the booth roof calls the next person.
- Left: the booth WINDOW with the person. Under it the grey-green COUNTER SHELF where they put their papers.
  Handing a paper back = dropping it ON THE PERSON, not on the shelf.
- Lower right: your dark DESK; papers are read there. The shelf left of the desk puts papers away.
- STAMP TRAY: a grey tab at the desk's right edge; dragged left it opens a bar with a red DENIED (left) and a
  green APPROVED (right) stamp. A stamp marks only the paper in the dark strip directly under THAT stamp.
- INSPECT MODE (desk darkened, red dotted frame): papers and stamps do not work; the red button at the lower right
  switches it.

2. INPUT: {input}

3. TODAY'S RULES
{rules}
Arstotzkans never need an entry ticket. A reading the rule needs that is not read yet is no reason to approve or
deny: get that paper read first.

4. GOAL FOR EACH ENTRANT
- Window empty -> click the loudspeaker (horn) to call the next person; waiting achieves nothing.
- Bring every paper the person hands over onto the desk where it can be read; read before deciding.
- The stamp you press is the one your own verdict names, with the passport lying under that stamp. One press.
- Then return every paper to the person: the entry ticket first, the passport LAST (they leave once it is back).
- Anything on the desk that is not theirs (flyer, citation slip, rulebook, bulletin) goes to the shelf first;
  it is never stamped or handed over alone.
- A person who hands over no documents gets no stamp: interrogate via inspect mode (rulebook on the desk ->
  Basic Rules page -> inspect button -> the rule line -> the empty counter -> the INTERROGATE prompt).

5. THINGS THAT WASTE TIME
- Dragging a stamp (stamps are clicked); re-pressing a stamp that already printed.
- Dropping papers under the tray bar (they hide behind it).
- Opening and closing the tray back and forth.
"""

DAY_RULE_TEXT = {
    "1": "Day 1 (1982.11.23): only Arstotzkans may enter. Issuing country (large letters at the bottom of the "
         "passport) ARSTOTZKA -> APPROVED; any other country -> DENIED. Expiry, city and tickets are not checked.",
    "2": "Day 2 (1982.11.24): foreigners may enter too, with a valid passport: not expired (EXP. after 1982.11.24) "
         "and the ISS. city listed in the rulebook for its country; otherwise DENIED.",
    "3": "Day 3 (1982.11.25): every passport must be valid: not expired (EXP. after 1982.11.25) and the ISS. city "
         "listed in the rulebook for its country. Foreigners also need an ENTRY TICKET VALID ON 1982.11.25; no "
         "ticket or another date -> DENIED.",
}

OTHER_SCREENS = """\
PAPERS, PLEASE -- SCREENS OUTSIDE THE BOOTH
Each turn: CLICK one numbered element, or WAIT while a screen fades.
- Main menu (title screen): click STORY.
- Day select ("Select day to continue or start a new game"): click the day tile with the HIGHEST day number (a fresh
  save has only DAY 1 / NEW). Never click BACK, QUIT or the trash icon (BACK undoes progress, trash deletes the
  save). If no tile is drawn yet, wait. A clicked tile opens a box with CONTINUE and CANCEL: click CONTINUE (the
  upper short line; OCR may misspell it, e.g. 'COHTIHUE').
- Intro, newspaper and bulletin screens: NEXT; then WALK TO WORK goes to the booth. Text without a button: click it.
- End of day (SAVINGS, RENT, HEAT, FOOD ..., a total, SLEEP): a NEGATIVE total ("$-5") means arrest for debt.
  Click HEAT, FOOD (and MEDICINE) one at a time to untick them until the total is zero or more, then SLEEP
  (RENT cannot be unticked). A total of zero or more: click SLEEP right away.
"""

# --------------------------------------------------------------------------
# request 1: state questions (answered from the UNMARKED frame)
# --------------------------------------------------------------------------

STATE_KEYS = ("person_at_window", "document_on_counter_shelf", "document_open_on_desk", "passport_open_readable",
              "stamp_tray_open", "passport_shows_stamp_mark", "bulletin_or_rulebook_covering_desk", "inspect_mode_on")
STRIP_KEYS = ("passport_under_denied", "passport_under_approved")
COUNTRIES = ("ARSTOTZKA", "KOLECHIA", "IMPOR", "ANTEGRIA", "OBRISTAN", "REPUBLIA", "UNITED FEDERATION")
DOC_KINDS = {"passport": "the entrant's passport (a small booklet, or its open pages: the ENTRY VISA page with an "
                         "empty stamp box on top, the data page with photo, name, DOB, SEX, ISS., EXP. and the "
                         "issuing country name in large letters)",
             "rulebook": "the inspector's rulebook (blue-grey cover 'RULES & REGULATIONS', or open pages: CONTENTS, "
                         "Basic Rules, Regional Map, Booth Info)",
             "bulletin": "the Ministry of Admission bulletin (a sheet with today's rules / news)",
             "entry_ticket": "an entry ticket (small slip with a date)",
             "transcript": "the interview transcript printout",
             "flyer": "a loose flyer / advertisement or note an entrant left (e.g. a pink 'The Pink Vice' card)",
             "citation": "an M.O.A. CITATION slip (a printed warning about a mistake; not the entrant's)",
             "other": "something else, or not a paper"}
# photo_matches_person is no longer asked (answers sat at p 0.4-0.68 all session; budget of 16 questions). The
# expiry check is TOD's reading of the EXP. year and month, compared with today's date by the rule (section 5).
# loop8: EXP. date and ISS. city are no longer request-1 questions; they are request-1b choices over the desk OCR
# (inspection_doc_questions below; eval day2_readings notes: expiry 3/30 -> 28/30, city 26/30 -> 30/30).
# loop11: photo_matches_person retired entirely (photo_eval notes: no wording separates match/mismatch).
INSPECT_KEYS = ("issuing_country",)
CHECK_KEYS = ("entry_ticket_dated_today",)
DENY_P = 0.75              # p a check answer needs before it can deny an entrant
CARRY_CHECK_MARGIN = 0.15   # a yes/no check is carried for the entrant when |p - 0.5| >= this
ISSUING_CITIES = {   # game rule (rulebook Regional Map): the passport's ISS. city must belong to its country
    "ARSTOTZKA": ("Orvech Vonor", "East Grestin", "Paradizna"),
    "ANTEGRIA": ("St. Marmero", "Glorian", "Outer Grouse"),
    "IMPOR": ("Enkyo", "Haihan", "Tsunkeido"),
    "KOLECHIA": ("Yurko City", "Vedor", "West Grestin"),
    "OBRISTAN": ("Skal", "Lorndaz", "Mergerous"),
    "REPUBLIA": ("True Glorian", "Lesrenadi", "Bostan"),
    "UNITED FEDERATION": ("Great Rapid", "Shingleton", "Korista City"),
}
CITY_TABLE = "; ".join(f"{c}: {', '.join(v)}" for c, v in ISSUING_CITIES.items())

STATE_TEXT = ("A screenshot of the game Papers, Please (border inspection booth). Answer each question only "
              "from what is visible in this picture.")


def _noul(instr: str, yes: str, no: str) -> dict:
    return {"type": "noul", "instructions": instr, "criteria": {"true": yes, "false": no}}


NO_DOCS_KEYS = ("rulebook_page", "interrogate_prompt_visible")   # step N sub-states, asked only around step N
NO_DOCS_DATES = (DAY_DATES["3"],)   # days 1-3: only Jorji (Day 3) presents no documents
NO_DOCS_P = 0.6   # p(no_documents_presented) for step N; also drops the passport-inspection questions
RULEBOOK_PAGES = {"not_open": "the rulebook is not lying open on the desk (closed in its slot, or not visible)",
                  "contents": "it is open on the CONTENTS page (list of sections)",
                  "basic_rules": "it is open on the BASIC RULES page (rule lines such as 'Entrant must have a "
                                 "passport')",
                  "regional_map": "it is open on the REGIONAL MAP page",
                  "booth_info": "it is open on the BOOTH INFO / document examples pages",
                  "other": "it is open on some other page, or the page cannot be told"}


def no_docs_questions() -> dict:
    """Request 1, step N (no documents presented): the sub-states of the missing-document interrogation."""
    return {
        "rulebook_page": {"type": "choice",
                          "instructions": "Is the inspector's rulebook (RULES & REGULATIONS) lying open on the desk, "
                                          "and if so which page is shown?",
                          "criteria": dict(RULEBOOK_PAGES)},
        "interrogate_prompt_visible": _noul(
            "Is an INTERROGATE prompt / button visible (it appears below the counter after a rule line and the "
            "empty counter were selected in inspect mode)?",
            "yes - an interrogate prompt or button is visible",
            "no - no interrogate prompt is visible"),
    }


def state_questions(today: str = DAY_DATES["1"], inspect: tuple = INSPECT_KEYS, prev: dict | None = None) -> dict:
    """The facts the manual is keyed on, asked as one TOD request over the plain
    frame. All noul except issuing_country (choice). `inspect` selects which
    inspection questions are asked (loop.py gates them on the desk OCR). `prev` = last tick's TOD answers
    (request 1 runs before this tick's answers exist): they gate WHICH questions are asked, never an answer --
    no_documents_presented while a person was at the window (or nothing is known yet), the step-N sub-state
    questions instead of the passport inspection while TOD said no documents were presented."""
    q = {
        "person_at_window": _noul(
            "Look at the left third of the picture, behind the window with the height marks 1.1 to 1.9. "
            "Is a person's head and shoulders visible there?",
            "yes - a person's head and shoulders are visible behind that window",
            "no - only the empty dark wall with height marks is visible there"),
        "document_on_counter_shelf": _noul(
            "Look at the grey-green counter shelf directly under the booth window (left side). Does a "
            "passport or other document lie on that shelf? The dark strip along the very bottom edge of the "
            "picture (rulebook, bulletin, booklets, clock, date) is the desk edge, not the shelf: things there "
            "do not count.",
            "yes - a passport/document lies on the grey-green counter shelf under the window",
            "no - the grey-green counter shelf is empty (bottom-edge desk objects do not count)"),
        "document_open_on_desk": _noul(
            "Look at the dark desk: the whole lower part of the picture to the right of the grey-green counter "
            "shelf (bottom-centre and bottom-right). Is an open passport (cream page with a name, numbers, a photo or "
            "an ENTRY VISA box) lying there, even if only a thin strip sticks out above or beside the grey stamp "
            "bar? The bulletin (dark blue sheet) and the rulebook do not count.",
            "yes - an open passport page lies on the desk (bottom-centre or bottom-right)",
            "no - no passport page anywhere on the desk"),
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
            "The rulebook (pages with CONTENTS / Basic Rules / Regional Map), the bulletin, an entry ticket (small slip, VALID ON ...) or any other paper is NOT "
            "a passport.",
            "yes - the entrant's passport lies under the DENIED stamp",
            "no - nothing, or a paper that is not the passport (rulebook, bulletin ...), lies under the DENIED stamp, "
            "or there is no stamp bar"),
        "passport_under_approved": _noul(
            "Look at the dark band directly BELOW the green APPROVED stamp (under the grey bar with the words ALIGN "
            "VISA BENEATH STAMP). Is the paper lying in that band, under the APPROVED stamp, the entrant's PASSPORT? "
            "The rulebook (pages with CONTENTS / Basic Rules / Regional Map), the bulletin, an entry ticket (small slip, VALID ON ...) or any other paper is NOT "
            "a passport.",
            "yes - the entrant's passport lies under the APPROVED stamp",
            "no - nothing, or a paper that is not the passport (rulebook, bulletin ...), lies under the APPROVED "
            "stamp, or there is no stamp bar"),
        # passport_shows_stamp_mark (yes/no) is no longer asked: 'stamped' is TOD's STAMP_INK_Q choice (audit B19)
        "bulletin_or_rulebook_covering_desk": _noul(
            "Does an open bulletin (Ministry of Admission sheet) or the rulebook lie ON TOP of the passport, "
            "hiding part of it? A bulletin lying next to the passport without covering it does not count.",
            "yes - a bulletin/rulebook covers part of the passport",
            "no - nothing covers the passport (or there is no passport)"),
        "inspect_mode_on": _noul(
            "Is the game in INSPECT MODE: the desk and booth are darkened, a red dotted frame surrounds them and "
            "red text HIGHLIGHT DISCREPANCIES shows at the lower right?",
            "yes - darkened desk with a red dotted frame and the words HIGHLIGHT DISCREPANCIES",
            "no - normal bright desk, no red dotted frame"),
        "issuing_country": {
            "type": "choice",
            "instructions": "Issuing country of the open passport: the country name printed in large letters on "
                            "the passport (bottom of the data page / cover). The rulebook and bulletin mention "
                            "'Arstotzkan' too -- only the PASSPORT counts.",
            "criteria": {**{c: f"the passport is issued by {c}" for c in COUNTRIES},
                         "unreadable": "no open passport, or its country name cannot be read"}},
        # 3-way choices: the old yes/no 'no' option also meant 'unreadable' and denied a valid Arstotzkan
        # (run 024712 t0: expiry 'no' p=0.96 with the page not readable)
        # run 033308 t49: a one-step "is EXP. after today?" answer said 'expired' (p=0.76) for a valid passport;
        # TOD now only READS the year and month, the loop compares them with today
        # run 042018 (Sazar Parvak, Day 2): the photo was someone else (game error Passport/Face) and was approved.
        # Only a confident 'different' (p >= DENY_P) denies; an unsure answer never blocks.
        "photo_matches_person": {
            "type": "choice",
            "instructions": "Compare the small photo on the open passport data page with the face of the person "
                            "standing at the booth window: face shape, hair, beard, glasses, head cover.",
            "criteria": {"match": "both are visible and show the same person",
                         "different": "both are visible and clearly show two different people",
                         "cannot_compare": "the passport photo or the person is not visible"}},
    }
    if today == DAY_DATES["3"]:   # Day 3: foreigners also need an entry ticket dated today
        # run 070911 t74-79 (Mattias Brooks, gt APPROVED): the ticket lay on the counter shelf, the old yes/no
        # question said 'no' (0.84) and he was DENIED. Now a 3-way choice; only a confident 'other_date' denies.
        q["entry_ticket_dated_today"] = {
            "type": "choice",
            "instructions": f"Today is {today}. Find the ENTRY TICKET (a small slip with the words ENTRY TICKET and "
                            f"'VALID ON' followed by a date) on the desk or the counter shelf. Which date is printed "
                            f"after 'VALID ON'?",
            "criteria": {"dated_today": f"an entry ticket is visible and its date reads VALID ON {today}",
                         "other_date": f"an entry ticket is visible and its date is clearly NOT {today}",
                         "no_ticket": "the entrant's papers are on the desk/counter and there is NO entry ticket "
                                      "among them",
                         "not_readable": "an entry ticket may be there but its date cannot be read in this picture"}}
    # step N only on the days with a scripted entrant who presents nothing (Day 3 entrant 8, docs/game.md); loop10
    # run 161058 Day 1: TOD said no documents (0.64-0.75) for empty windows and arriving entrants -> 22 N ticks
    if today in NO_DOCS_DATES and (prev is None or yes(prev, "person_at_window")):
        q["no_documents_presented"] = _noul(
            "Look at the person at the booth window and the counter shelf in front of them. Has the person handed "
            "over no documents at all: the counter in front of them is empty and nothing of theirs lies on the "
            "desk? (Only the inspector's own things -- rulebook, bulletin, citation slips -- may be on the desk.)",
            "yes - the person at the window has handed over no documents: the counter in front of them is empty "
            "and nothing of theirs is on the desk",
            "no - the person has handed over a passport or other papers (on the counter or on the desk), or no "
            "person is at the window")
    if prev is not None and yes(prev, "no_documents_presented", NO_DOCS_P):
        q.update(no_docs_questions())
        inspect = ()   # no passport to inspect: those answers would be meaningless
    return {k: v for k, v in q.items() if k not in INSPECT_KEYS or k in inspect}


# 'Stamped' is TOD's answer to STAMP_INK_Q (audit B19): asked while a passport can carry ink (tray open with a paper,
# a stamp press on record, or ink read before; loop.probe_context). A stamp press that changed pixels is only history.
MARK_SIDE_P = 0.75   # p the ink-side choice needs to count (163640 dry run: the yes/no mark question sat at
# 0.20-0.54 on the inked Uvilia passport while this choice read DENIED 0.53-0.84 on ticks 8-15, >= 0.80 on 11, 12, 15)
MARK_NOPRESS_P = 0.85   # without a stamp press on record for this entrant (restart) the ink answer needs this p
STAMP_INK_Q = {"type": "choice",
               "instructions": "Look at the entrant's passport wherever it lies (on the desk or in the dark strip under "
                               "the stamp tray). Is a stamp ink mark printed on its page? The two big stamps sitting on "
                               "the grey tray bar are NOT marks.",
               "criteria": {"approved": "a green APPROVED ink mark is printed on the passport page",
                            "denied": "a red DENIED ink mark is printed on the passport page",
                            "none": "no stamp ink on the passport, or no passport page is visible"}}


def mark_side_answer(state: dict) -> dict | None:
    """{'value': 'approved'|'denied', 'p'} when the recheck choice (STAMP_INK_Q) is confident."""
    a = state.get("passport_stamp_ink")
    if a and a["value"] in ("approved", "denied") and a["p"] >= MARK_SIDE_P:
        return {"value": a["value"], "p": a["p"]}
    return None


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
    "inspect_mode_on": "Inspect mode is ON (desk darkened, red dotted frame, HIGHLIGHT DISCREPANCIES)",
    "no_documents_presented": "The person at the window has handed over no documents (counter empty, nothing of "
                              "theirs on the desk)",
    "interrogate_prompt_visible": "An INTERROGATE prompt is visible",
}


_CHECK_LABEL = {"expiry_after_today": "Passport expiry date is after today",
                "photo_matches_person": "Passport photo matches the person at the window",
                "issuing_city_valid": "Passport ISS. city belongs to the passport's country",
                "entry_ticket_dated_today": "An entry ticket dated today is visible"}


DOC_KINDS_TEXT = {
    "passport": "the entrant's passport: text such as ENTRY VISA, a 'Surname, Given' name, DOB./DOE., SEX, ISS. or EXP. "
                "dates and a country name (ARSTOTZKA, KOLECHIA, IMPOR, REPUBLIA, ...)",
    "rulebook": "the inspector's rulebook: RULES & REGULATIONS cover, or pages headed CONTENTS, Basic Rules, Regional Map, "
                "Booth Info",
    "bulletin": "the Ministry of Admission bulletin: a sheet of today's rules / news",
    "entry_ticket": "a small slip reading ENTRY TICKET with a VALID ON date (often ARSTOTZKA as its header)",
    "transcript": "the interview transcript printout (lines of dialogue)",
    "flyer": "an advert reading The Pink Vice (a pink card with an East Grestin address and FOR ALL YOUR FANTASIES)",
    "citation": "an M.O.A. CITATION slip reading CITATION / Protocol Violated / WARNING ISSUED / NO PENALTY",
    "other": "none of these, or not a paper",
}
# identity gate (ticket_flyer_identity notes): textless counter papers came back at p <= 0.32 on every tick
# (chance 0.125) and were still used as 'entry ticket' / 'flyer' (182519 t118-119, t125-134). An identity answer
# below this p is 'unread': the paper is a document on the counter/desk, never a ticket, flyer or passport for any step.
IDENTITY_MIN_P = 0.4
UNREAD = "unread"


def doc_question(d: dict) -> dict:
    """Request-1 identity question for one paper: its OCR lines verbatim, each choice defined by its printed text
    (A/B run 20261003_182519: text papers 18/18, p(gt) 0.90 -> 0.93 vs the previous wording)."""
    where = "on the counter shelf under the booth window" if d["where"] == "counter" else "on the dark desk"
    tx = d.get("text") or []
    ocr = ("OCR read these lines inside it, verbatim (pixel font, may contain misreads):\n"
           + "\n".join(f"  | {t}" for t in tx)) if tx else \
        "OCR read no text inside it (counter papers are too small to read); judge from the picture only."
    return {"type": "choice",
            "instructions": f"Look at the paper lying {where}, at the {d['pos']} of the picture. {ocr}\n"
                            f"What is this paper? Each choice is defined by the text printed on it.",
            "criteria": dict(DOC_KINDS_TEXT)}


# hand-back state (audit B20): TOD's answer on the frame replaces the old tick windows (HANDBACK_STAY 4 /
# HANDBACK_DOCS_STAY 14) and the Day-3 combination rule
PASSPORT_RETURNED_Q = {
    "type": "choice",
    "instructions": "Look at the booth window (left), the counter shelf under it, the dark desk and the strip under "
                    "the stamp tray. Does the entrant at the window still have to get their PASSPORT back from the "
                    "inspector?",
    "criteria": {"returned": "a person is at the window and their passport is no longer on the counter shelf, the desk "
                             "or under the stamp tray (it was handed back); other small papers may still lie there",
                 "still_here": "the entrant's passport (closed booklet or open pages) still lies on the counter shelf, "
                               "the desk or under the stamp tray",
                 "no_person": "nobody is standing at the booth window"}}
RETURNED_P = 0.6   # p the 'returned' / 'still_here' answer needs before the entrant memory uses it


def returned_answer(state: dict) -> str | None:
    a = state.get("passport_returned")
    return a["value"] if a and a["p"] >= RETURNED_P else None


# ---- the verdict: TOD's own answer (audit A1-A4) ------------------------------------------------------------------
VERDICT_P = 0.5   # p the verdict answer needs before the state block calls it TOD's verdict
VERDICT_RULES = {
    "1": "Day 1 (1982.11.23): only Arstotzkan citizens may enter. Issuing country ARSTOTZKA -> APPROVED; any other "
         "country -> DENIED. Expiry, city and tickets are NOT checked today.",
    "2": "Day 2 (1982.11.24): foreigners are allowed too. APPROVED if the passport is valid: it is not expired (its EXP. "
         "date is after 1982.11.24) AND its ISS. (issuing) city is in the rulebook list for the passport's country; "
         "otherwise DENIED. Rulebook cities: " + CITY_TABLE + ".",
}
VERDICT_RULES["3"] = (VERDICT_RULES["2"].replace("Day 2 (1982.11.24): foreigners are allowed too.",
                                                 "Day 3 (1982.11.25): the Day 2 passport rule still holds:")
                      .replace("1982.11.24", "1982.11.25")
                      + " NEW TODAY: a foreigner (any country other than ARSTOTZKA) also needs an ENTRY TICKET VALID ON "
                        "1982.11.25; no ticket, or a ticket with another date -> DENIED. Arstotzkans need no ticket.")


def _reading(r: dict | None, fmt) -> str:
    return f"{fmt(r)} (your answer at tick {r['tick']}, p={r['p']:.2f})" if r else "not read yet"


def verdict_question(day: str, mem: dict, counter_doc: bool = True) -> dict:
    """Request-1b `verdict` choice: today's rule as text + the readings TOD itself gave for this entrant on earlier
    ticks (`mem`: country / exp / city / ticket, each {value, p, tick} or None), labelled as TOD's own answers. TOD
    applies the rule; no code compares anything."""
    d = day if day in VERDICT_RULES else "1"
    lines = [f"- issuing country: {_reading(mem.get('country'), lambda r: r['value'])}"]
    if d in ("2", "3"):
        lines.append(f"- EXP. date: {_reading(mem.get('exp'), lambda r: r['value'])}")
        lines.append(f"- ISS. city: {_reading(mem.get('city'), lambda r: repr(r['value']))}")
    if d == "3":
        lines.append(f"- entry ticket: {_reading(mem.get('ticket'), lambda r: r['value'])}")
    return {"type": "choice",
            "instructions": "You are the border inspector deciding the entrant at the window. TODAY'S RULE: "
                            + VERDICT_RULES[d] + "\nYOUR OWN EARLIER READINGS of this entrant's papers (answers you "
                            "gave on earlier ticks; check them against the picture, the papers may be visible now):\n"
                            + "\n".join(lines) + "\nApply today's rule. If a reading the rule needs is not read yet "
                            "and is not readable in this picture either, answer cannot_decide_yet."
                            + ("\nA paper still lying on the counter shelf (below the window, not yet dragged onto the "
                               "desk) has not been read and may be the entry ticket: while any paper lies on the "
                               "counter, 'no entry ticket' is not a final reading -- answer cannot_decide_yet."
                               if d == "3" and counter_doc else ""),
            "criteria": {"approved": "APPROVED: the entrant's papers meet today's rule",
                         "denied": "DENIED: the entrant's papers break today's rule",
                         "cannot_decide_yet": "a reading today's rule needs (country, EXP. date, ISS. city or, for a "
                                              "foreigner on Day 3, the entry ticket) is not read yet or unreadable "
                                              "(a reading of 'no entry ticket among the papers' IS a reading: the "
                                              "rule decides it)"}}


def tod_verdict(state: dict, facts: dict | None) -> dict | None:
    """TOD's verdict answer: this tick's (request 1b), else the last one carried for this entrant.
    {'value': 'approved'|'denied'|'cannot_decide_yet', 'p', 'where'} or None when never asked."""
    v = state.get("verdict")
    if v:
        return {"value": v["value"], "p": v["p"], "where": "this frame"}
    c = (facts or {}).get("verdict_carried")
    return {"value": c["value"], "p": c["p"], "where": f"tick {c['tick']}"} if c else None


def verdict_side(state: dict, facts: dict | None) -> str | None:
    """'approved'/'denied' when TOD's verdict answer names one at p >= VERDICT_P (diagnostics / F2 only)."""
    v = tod_verdict(state, facts)
    return v["value"] if v and v["value"] in ("approved", "denied") and v["p"] >= VERDICT_P else None


def _pp_phrase(v: dict) -> str:
    """'the PASSPORT (p=0.81)' -- the p of the branch that made it the under-strip fact (loop20)."""
    if v.get("source") == "tod_identity+pixel":
        return f"the PASSPORT (your identity of the paper there: passport, p={(v.get('doc_p') or 0):.2f})"
    return f"the PASSPORT (p={(v.get('passport_p') or 0):.2f})"


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


NO_DOCS_HINT_RE = re.compile(r"NO DOCUMENTS|INSPECT mode|to interrogate", re.I)


DESK_LINE_MAX = 120    # loop21: request-2 desk OCR line cap (chars)
DESK_TEXT_MAX = 600    # loop21: request-2 desk OCR block cap (chars of OCR)
HIST_MAX = 15          # loop21: request-2 history lines (run 004042 t115: 29k chars -> TOD 413 state_too_long)
HIST_LINE_MAX = 140    # loop21: chars per history line
STATE_BUDGET = 7000    # loop21: hard cap (chars) on the request-2 text after the manual (state + desk + history + ruled out); the Day-3 booth manual alone is ~17k, so the whole text stays <= ~24k (413 at ~29k with the image, run 004042 t115)


def short_text(t: str, n: int) -> str:
    return t if len(t) <= n else t[: n - 3] + "..."


_HIST_ABBR = (("passport under ", "under "), (" pressed t", " t"), ("ink read ", "ink "),
              ("doc on counter", "doc counter"), ("passport on desk", "pp desk"), ("tray open", "tray"))


def hist_line(x: str, n: int = HIST_LINE_MAX) -> str:
    """One history line cut to n chars: tick | what was true | input | element short name | effect."""
    f = x.split(" | ")
    if len(f) < 5:
        return short_text(x, n)
    tk, ssum, inp, eff = f[0], f[1], f[2], f[-1]
    el = " | ".join(f[3:-1])

    def nm(e: str) -> str:   # element short name: the label before its TOD p / OCR / description tail
        e = e.strip().strip("'").rstrip(".")
        for pre in ("drop target - ", "object — ", "text — ", "object � ", "text � ",
                    "no element - "):
            if e.startswith(pre):
                e = e[len(pre):]
        if e.startswith("stamp landing strip (under the "):
            e = ("APPROVED" if e[31:33] == "AP" else "DENIED" if e[31:33] == "DE" else "?") + " strip"
        mt = re.match(r"(\w+) \(TOD (\d\.\d+)\)", e)
        if mt:   # 'passport (TOD 0.94) -- SER ...' -> 'passport 0.94'
            return f"{mt.group(1)} {mt.group(2)}"
        e = e.replace(" — ", ": ").replace(" � ", ": ")
        for sep in (" -- ", " ("):
            if sep in e and e.index(sep) > 3:
                e = e[: e.index(sep)]
        return short_text(e.rstrip(" -:"), 34)
    if " -> " in el:
        a, b = el.split(" -> ", 1)
        el = f"{nm(a)} -> {nm(b)}"
    else:
        el = nm(el)
    eff = short_text(eff, 26)
    for a, b in _HIST_ABBR:
        ssum = ssum.replace(a, b)
    room = n - len(f"{tk} |  | {inp} | {el} | {eff}")
    return f"{tk} | {short_text(ssum, max(room, 10))} | {inp} | {el} | {eff}"[:n]


def desk_text_block(facts: dict | None, cap: bool = False) -> str:
    """'READABLE TEXT ON THE DESK' -- the OCR'd text of every document on the desk
    (extraction output, sent to both requests). cap (request 2, loop21 413 fix): the OCR of a paper TOD stowed
    (step K) is dropped, each line is cut to DESK_LINE_MAX and the block to ~DESK_TEXT_MAX chars of OCR."""
    facts = facts or {}
    full = list(facts.get("desk_text") or [])
    lines = full
    if cap:
        if facts.get("stowed"):
            gone = {t for d in facts.get("docs") or [] if d.get("where") == "counter" for t in d.get("text") or []}
            lines = [t for t in lines if t not in gone]
        kept, n = [], 0
        for t in lines:
            t = short_text(t, DESK_LINE_MAX)
            if n + len(t) > DESK_TEXT_MAX:
                break
            kept.append(t)
            n += len(t)
        lines = kept
    if not lines:
        return "READABLE TEXT ON THE DESK (OCR of the documents on the desk): none"
    out = ["READABLE TEXT ON THE DESK (OCR of the documents on the desk, top to bottom; pixel font, may "
           "contain misreads):"]
    out += [f"- {t}" for t in lines]
    hint = [t for t in full if NO_DOCS_HINT_RE.search(t)]
    if hint:   # the game's own slip: 'THIS ENTRANT HAS NO DOCUMENTS / To proceed, use INSPECT mode to interrogate'
        out.append("GAME HINT ON SCREEN (OCR): " + " / ".join(hint))
    return "\n".join(out)


def ink_now(state: dict, facts: dict | None) -> dict | None:
    """This tick's STAMP_INK_Q answer when it names an ink side confidently enough (MARK_SIDE_P with a stamp press
    on record for this entrant, MARK_NOPRESS_P without)."""
    f = facts or {}
    ms = mark_side_answer(state)
    if ms and (f.get("stamp_clicks") or f.get("missed_stamps") or ms["p"] >= MARK_NOPRESS_P):
        return ms
    return None


def stamped(state: dict, facts: dict | None) -> list[str]:
    """['pressed'] once TOD's stamp press was executed for this entrant (a stamp TOD picked, the passport under it
    by TOD's strip answer, the input sent: entrant memory `stamp_clicks`), else [] (manual rule F). User decision
    loop16 (runs 210453 t20 / 211624 t17: the APPROVED press inked the visa page, STAMP_INK_Q read 'none' 0.59-0.89,
    no hand-back until 18:00): the ink reading is an informative fact in the state block, never the gate."""
    return ["pressed"] if (facts or {}).get("stamp_clicks") else []


def pressed_side(facts: dict | None) -> str | None:
    """Side of the last executed stamp press for this entrant."""
    sc = (facts or {}).get("stamp_clicks") or []
    return sc[-1][1] if sc else None


def ink_side(state: dict, facts: dict | None) -> str | None:
    """Which ink TOD read (informative only, see stamped)."""
    ms = ink_now(state, facts) or (facts or {}).get("mark_side")
    return ms["value"] if ms else None


def known_country(state: dict, facts: dict | None):
    """(value, p, where) of the issuing country: this frame's reading if it was asked, else the most recent
    confident reading carried from earlier ticks of this entrant, else None."""
    c = state.get("issuing_country")
    if c and c["value"] != "unreadable" and c["p"] >= 0.6:   # weak readings (run 022439 t74: 0.24) do not count
        return c["value"], c["p"], "this frame"
    cc = (facts or {}).get("country_carried")
    if cc:
        return cc["value"], cc["p"], f"tick {cc['tick']}"
    return None


def known_city(state: dict, facts: dict | None):
    """(city, p, where): a rulebook name, or the passport's own (non-rulebook) spelling read at p >= DENY_P."""
    c = state.get("issuing_city")
    if c and c["value"] != "unreadable" and c["p"] >= (0.6 if c["value"] in _ALL_CITIES else DENY_P):
        return c["value"], c["p"], "this frame"
    cc = (facts or {}).get("city_carried")
    if cc:
        return cc["value"], cc["p"], f"tick {cc['tick']}"
    return None


CHECK_YES = {"valid": True, "match": True, "expired": False, "different": False,
             "dated_today": True, "other_date": False, "no_ticket": False}   # run 092642: Mahovski had no ticket


def check_answer(a: dict | None):
    """True/False/None from one request-1 answer of a check (3-way choice or yes/no). Only carries TOD's reading
    for the verdict question's text; the same p is needed either way (audit D6: no approve/deny asymmetry)."""
    if not a:
        return None
    if isinstance(a.get("value"), str) and a["value"] not in ("True", "False"):
        v = CHECK_YES.get(a["value"])
        return v if v is not None and a["p"] >= 0.6 else None
    return (a["p"] >= 0.5) if abs(a["p"] - 0.5) >= CARRY_CHECK_MARGIN else None


def known_exp(state: dict, facts: dict | None):
    """(YYYY.MM.DD, p, where) of the EXP. date TOD picked among the OCR dates (this frame, else carried), or None."""
    e = state.get("exp_read")
    if e and e["p"] >= 0.5:
        return e["value"], e["p"], "this frame"
    c = (facts or {}).get("exp_carried")
    return (c["value"], c["p"], f"tick {c['tick']}") if c else None




# ---- request 1b: Day 2/3 readings as choices over the strings the OCR read on the screen ----------------------
# (private eval 2026-10-03, 30 gt-labelled Day 2 frames: expiry 3/30 -> 28/30 right, 0 wrong; city 26/30 -> 30/30)
_DATE_RE = re.compile(r"(19\d\d)[.,](\d\d)[.,](\d\d)")
_VALID_RE = re.compile(r"VA[L1I][I1L]D|VALID|ENTRY\s*T[I1]CKET", re.I)   # pixel-font OCR may swap I/L/1
_ISS_RE = re.compile(r"(?:^|[\s.;,])(?:[I1lUu]?[S5s$][S5s$]\.?)\s*([A-Za-z][A-Za-z.' ]*?)\s*"
                     r"(?=\bE[XNR]?P\b|\bE[XNR]?P[.\d ]|\bP\.\d|\d|ARSTOT|$)")
_ALL_CITIES = [c for v in ISSUING_CITIES.values() for c in v]


def _flat(s: str) -> str:
    return s.lower().replace(".", "").replace(" ", "")


def ocr_dates(lines: list[str]) -> list[str]:
    """Every plausible full date the desk OCR read (YYYY.MM.DD), reading order, de-duplicated."""
    out = []
    for t in lines:
        for m in _DATE_RE.finditer(t):
            d = ".".join(m.groups())
            if 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(3)) <= 31 and d not in out:
                out.append(d)
    return out[:8]


def ocr_city_tokens(lines: list[str], k: int = 6) -> list[str]:
    """City-like OCR strings: text after an ISS.-like marker + 1-2 word windows resembling any rulebook city
    (ratio >= 0.75). The strings keep the OCR's spelling; they are never replaced by the rulebook spelling."""
    raw = []
    for t in lines:
        raw += [m.group(1) for m in _ISS_RE.finditer(t)]
        w = re.findall(r"[A-Za-z][A-Za-z.']*", t)
        for n in (1, 2):
            for i in range(len(w) - n + 1):
                s = " ".join(w[i:i + n]).strip(".")
                if len(s) >= 4 and max(difflib.SequenceMatcher(None, s.lower(), c.lower()).ratio()
                                       for c in _ALL_CITIES) >= 0.75:
                    raw.append(s)
    out = []
    for s in raw:
        s = re.sub(r"^(?:(?!St[. ])[A-Za-z]{1,2}\.?\s+|[I1lUu]?[S5s$][S5s$]\.\s*)", "", s.strip(" ."))
        s = re.sub(r"\s+(?:E[XNR]?P|P)\.?$", "", s).strip(" .")
        if len(s) >= 3 and _flat(s) not in [_flat(x) for x in out]:
            out.append(s)
    out = [c for c in out if not any(o != c and o.lower().endswith(c.lower()) for o in out)]
    return out[:k]


def nearest_rule_city(tok: str) -> str:
    return max(_ALL_CITIES, key=lambda c: difflib.SequenceMatcher(None, _flat(tok), _flat(c)).ratio())


def inspection_doc_questions(desk_text: list[str], day: str = "2") -> tuple[dict, dict]:
    """Request-1b questions (Day 2/3) + the candidate lists needed to read the answers back. The options are the
    strings the OCR read on the screen; the spelling contrast is asked only when the first city token differs from
    the nearest rulebook name (neutral labels, OCR spelling first)."""
    # ticket options: only dates OCR read on a line that itself says VALID (run 205418 t17: the ticket had left the
    # desk OCR, the options were the passport's dates labelled 'VALID ON ...', TOD took D1 = EXP. 1984.03.10 at 0.78)
    tdates = ocr_dates([t for t in desk_text if _VALID_RE.search(t)])
    # EXP. options: not the dates OCR read on a VALID line (run 222257 t24 Henry Chau, gt APPROVED: TOD took the
    # ticket's 'VALID ON 1982.11.25' as the EXP. date at 0.76 -> expired today -> denied 0.72-0.80)
    q, cand = {}, {"dates": ocr_dates([t for t in desk_text if not _VALID_RE.search(t)]), "tdates": tdates,
                   "toks": ocr_city_tokens(desk_text)}
    if cand["dates"]:
        q["exp_date"] = {
            "type": "choice",
            "instructions": "OCR found these dates on the documents on the desk. On the open passport data page, "
                            "which one is printed after 'EXP.' (the expiry date; the date after 'DOB.' is the birth "
                            "date)?",
            "criteria": {**{f"D{i + 1}": f"EXP. {d}" for i, d in enumerate(cand["dates"])},
                         "none": "none of these is the passport's EXP. date, or no open passport data page is visible"}}
    if day == "3" and tdates:
        # run 092642 Troyer: ticket VALID ON 1982.12.09 read 'dated_today' by the yes/no-style question
        q["ticket_date"] = {
            "type": "choice",
            "instructions": "OCR read these 'VALID ON' lines on the desk. On the ENTRY TICKET (small slip with "
                            "'ENTRY TICKET' and 'VALID ON'), which one is printed after 'VALID ON'?",
            "criteria": {**{f"D{i + 1}": f"VALID ON {d}" for i, d in enumerate(tdates)},
                         "none": "none of these is the entry ticket's date, or no entry ticket is visible"}}
    if cand["toks"]:
        q["issuing_city_tok"] = {
            "type": "choice",
            "instructions": "OCR found these city-like words on the documents on the desk (pixel font; letters may be "
                            "misread). On the open passport data page, which one is the issuing city printed after "
                            "'ISS.'?",
            "criteria": {**{f"C{i + 1}": f"ISS. '{t}' (as read by OCR)" for i, t in enumerate(cand["toks"])},
                         "none": "none of these is the city printed after 'ISS.', or no open passport data page is "
                                 "visible"}}
        tok = cand["toks"][0]
        rule = nearest_rule_city(tok)
        if _flat(rule) != _flat(tok):
            cand["spell"] = (tok, rule)
            q["issuing_city_spelling"] = {
                "type": "choice",
                "instructions": "On the open passport data page, read the city printed after 'ISS.' letter by letter. "
                                "Which spelling is printed there exactly?",
                "criteria": {"S1": f"ISS. {tok}", "S2": f"ISS. {rule}",
                             "S3": "another spelling, or no open passport data page is visible"}}
    return q, cand


def _pick(a: dict | None, n: int) -> int | None:
    v = (a or {}).get("value") or ""
    return int(v[1:]) - 1 if v[1:].isdigit() and 1 <= int(v[1:]) <= n else None


def read_inspection_answers(state: dict, cand: dict) -> None:
    """1b answers -> state['exp_read'] {value: YYYY.MM.DD, p} and state['issuing_city'] {value, p}; the city value is
    a rulebook name, or the passport's own spelling when TOD reads the non-rulebook spelling (denies at DENY_P)."""
    i = _pick(state.get("exp_date"), len(cand["dates"]))
    if i is not None:
        state["exp_read"] = {"value": cand["dates"][i], "p": state["exp_date"]["p"]}
    td = cand.get("tdates", cand["dates"])
    i = _pick(state.get("ticket_date"), len(td))
    if i is not None:   # the ticket's date as picked among the OCR 'VALID ON' dates overrides the direct ticket answer
        same = td[i] == DAY_DATES["3"]
        state["entry_ticket_dated_today"] = {"value": "dated_today" if same else "other_date",
                                             "p": state["ticket_date"]["p"], "from": f"VALID ON {td[i]}"}
    a = state.get("issuing_city_tok")
    i = _pick(a, len(cand["toks"]))
    if i is None:
        return
    tok = cand["toks"][i]
    rule = nearest_rule_city(tok)
    if _flat(tok) == _flat(rule):
        state["issuing_city"] = {"value": rule, "p": a["p"]}
        return
    sp = state.get("issuing_city_spelling")
    if sp and cand.get("spell", (None,))[0] == tok:
        if sp["value"] == "S1":
            state["issuing_city"] = {"value": tok, "p": min(a["p"], sp["p"])}
        elif sp["value"] == "S2":
            state["issuing_city"] = {"value": rule, "p": min(a["p"], sp["p"])}










def state_block(state: dict, day: str, facts: dict | None = None) -> str:
    facts = facts or {}
    today = DAY_DATES.get(day, DAY_DATES["1"])
    lines = ["WHAT IS CURRENTLY TRUE ON SCREEN (read from this frame by a separate check; p = confidence):"]
    scr = state.get("screen")
    if scr:
        lines.append(f"- Screen: {scr['value']} (p={scr['p']:.2f})")
    for k in STATE_KEYS:
        if k == "passport_shows_stamp_mark":   # 'stamped' = TOD's ink reading (STAMP_INK_Q), never a pixel change
            ink = ink_now(state, facts)
            ms = ink or facts.get("mark_side")
            # informative only (user decision loop16): 'stamped' is the executed press (stamped / pressed_side)
            if ms:
                where = "this frame" if ink else f"tick {ms['tick']}"
                lines.append(f"- Stamp ink on the passport (informative): {ms['value'].upper()} (your reading, {where}, "
                             f"p={ms['p']:.2f})")
            elif "passport_stamp_ink" in state:
                a_ = state["passport_stamp_ink"]
                lines.append(f"- Stamp ink on the passport (informative): no ink side read (your reading this frame: "
                             f"{a_['value']}, p={a_['p']:.2f})" + (" -- a press is on record; if no mark shows, the "
                             "press may have missed" if facts.get("stamp_clicks") else ""))
            continue
        lines.append(f"- {_LABEL[k]}: {_yn(state, k)}")
    for k in ("no_documents_presented", "interrogate_prompt_visible"):
        if k in state:
            lines.append(f"- {_LABEL[k]}: {_yn(state, k)}")
    rp = state.get("rulebook_page")
    if rp:
        lines.append(f"- Rulebook page open on the desk: {rp['value'].upper()} (p={rp['p']:.2f})")
    for d in facts.get("docs_named") or []:
        where = "counter shelf" if d["where"] == "counter" else "desk"
        if d["id"] == UNREAD:
            st = facts.get("stowed") or []
            lines.append(f"- Paper on the {where} ({d['pos']}): UNREAD (what it is could not be read: best identity "
                         f"{d.get('raw_id', '?')} at only p={d['p']:.2f}). It is not known to be a ticket, flyer or "
                         "passport" + ((f"; at tick {st[-1][0]} you put the {st[-1][1]} away on this shelf "
                                        "-- it stays there")
                                       if where == "counter shelf" and st else
                                       "; on the desk it can be read" if where == "counter shelf" else ""))
            continue
        tail = (" -- the inspector's, not needed for this entrant"
                if d["id"] in ("rulebook", "bulletin") and d["where"] == "desk" and d["p"] >= 0.6
                and not (d["id"] == "rulebook" and no_passport(state, facts)) else "")   # step N reads it on the desk
        cl = next((c for c in facts.get("clutter") or [] if c["native"] == d.get("native")), None)
        if cl:
            tail = clutter_phrase(cl, yes(state, "stamp_tray_open") or bool(facts.get("tray_open_px")))
        lines.append(f"- Paper on the {where} ({d['pos']}): {d['id'].upper()} (p={d['p']:.2f}){tail}")
    if (facts.get("desk_target") or {}).get("target") == "desk_clear":
        # loop.passport_needs_clear_space: an open passport on the desk whose data page request 1/1b could not read
        c = state.get("issuing_country") or {}
        why = [f"issuing country read as {c.get('value', 'unknown')} (p={c.get('p', 0):.2f})"]
        if day in ("2", "3") and not known_exp(state, facts):
            why.append("EXP. date not read")
        lines.append("- Passport data page readable: no (" + "; ".join(why) + "); the 'clear desk space' target "
                     "moves it so the whole page shows")
    by_side: dict[str, list] = {}
    for t, side in facts.get("stamp_clicks") or []:
        by_side.setdefault(side.upper(), []).append(t)
    for side, ts in by_side.items():
        lines.append(f"- Stamp pressed: {side} at tick{'s' if len(ts) > 1 else ''} {', '.join(map(str, ts[-4:]))} "
                     "(your press with the passport under it: it counts as stamped)")
    for t, side in facts.get("missed_stamps") or []:
        lines.append(f"- The {side.upper()} stamp was clicked at tick {t} while the passport lay under the other "
                     "stamp: nothing was stamped")
    if "strip" in facts and (yes(state, "stamp_tray_open") or facts.get("tray_open_px")):
        pu = facts.get("passport_under") or []
        for side in ("denied", "approved"):
            lines.append(f"- Under the {side.upper()} stamp: " + (
                _pp_phrase(facts["strip"][side]) if side in pu else under_phrase(facts, side)))
        lines.append("- The passport is under: " + (" and ".join(x.upper() for x in pu) if pu else "none")
                     + " (only the stamp your verdict names is offered)")
    pop = facts.get("paper_on_passport")
    if pop:
        lines.append(f"- A paper ({pop['id'].upper()}) lies across the open passport on the desk (covers "
                     f"{int(round(100 * pop['covered']))}% of it)")
    tr = ticket_to_return(state, facts)
    if stamped(state, facts) and tr:
        lines.append(f"- The entrant's ENTRY TICKET still lies on the {tr}")
    v = tod_verdict(state, facts)
    if v:
        lines.append(f"- Your verdict for this entrant (your own answer, {v['where']}): {v['value'].upper()} "
                     f"(p={v['p']:.2f})")
    ra = state.get("passport_returned")
    if facts.get("waiting_docs"):
        said = f"you said 'returned', p={ra['p']:.2f}" if ra else "your answer on an earlier tick"
        lines.append(f"- The entrant's passport has been handed back ({said}) and the entrant is STILL at the window "
                     "with papers of theirs on the desk or counter shelf")
    elif facts.get("handed_back") is not None:
        lines.append(f"- Documents were handed back at tick {facts['handed_back']}: this entrant is finished")
    open_ok = yes(state, "passport_open_readable")
    c = state.get("issuing_country")
    if c:
        lines.append(f"- Passport issuing country (your reading, this frame): {c['value']} (p={c['p']:.2f})")
    elif open_ok:
        lines.append("- Passport issuing country: not readable in this frame")
    cc = facts.get("country_carried")
    if cc and cc.get("tick") != facts.get("tick"):
        lines.append(f"- Passport read as {cc['value']} at tick {cc['tick']} (your reading, p={cc['p']:.2f})")
    if day in ("2", "3"):
        e = known_exp(state, facts)
        lines.append(f"- Passport EXP. date (your reading, {e[2]}, p={e[1]:.2f}): {e[0]}; today is {today}" if e
                     else "- Passport EXP. date: not read yet")
        ci = known_city(state, facts)
        lines.append(f"- Passport ISS. city (your reading, {ci[2]}, p={ci[1]:.2f}): {ci[0]}" if ci
                     else "- Passport ISS. city: not read yet")
    for k, c in ((facts.get("checks_carried") or {}).items() if day in ("2", "3") else ()):
        if k not in state:
            lines.append(f"- {_CHECK_LABEL[k]}: {'yes' if c['value'] else 'no'} (your reading at tick {c['tick']}, "
                         f"p={c['p']:.2f})")
    tc = (facts.get("checks_carried") or {}).get("entry_ticket_dated_today") or {}
    if (day == "3" and (state.get("entry_ticket_dated_today") or {}).get("value") == "no_ticket"
            and tc.get("raw") in ("dated_today", "other_date")):   # a read ticket stays read (loop.Entrant.update)
        lines.append(f"- Entry ticket (your reading at tick {tc['tick']}, p={tc['p']:.2f}): {tc['raw']}"
                     + (f" ({tc['from']})" if tc.get("from") else "")
                     + "; it is not in view now (moved or covered), the reading still holds")
    elif day == "3" and "entry_ticket_dated_today" in state:
        a_ = state["entry_ticket_dated_today"]
        lines.append(f"- Entry ticket (your reading, this frame): {a_['value']}"
                     + (f" ({a_['from']})" if a_.get("from") else "") + f" (p={a_['p']:.2f})")
    lines.append(f"- Day: {day} (today {today})" if day in DAY_RULES
                 else "- Day: not yet known (treat as day 1 until a later date shows)")
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
    if stamped(state, facts):
        t_, sd_ = (facts or {})["stamp_clicks"][-1]
        bits.append(f"STAMPED({sd_.upper()} pressed t{t_})")
    ik = state.get("passport_stamp_ink")
    if ik:
        bits.append(f"ink read {ik['value']} p={ik['p']:.2f}")
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
    by_side: dict[str, list] = {}
    for t, side in facts.get("stamp_clicks") or []:
        by_side.setdefault(side.upper(), []).append(t)
    for side, ts in by_side.items():   # loop21: one bit per side (t114: 11 repeated 'stamped APPROVED at tick N')
        ts_s = ", ".join(str(t) for t in ts[-4:])
        bits.append(f"stamped {side} at tick{'s' if len(ts) > 1 else ''} {ts_s}"
                    + (f" ({len(ts)} presses)" if len(ts) > 4 else ""))
    if facts.get("handed_back") is not None:
        bits.append(f"handed back at tick {facts['handed_back']}")
    return "THIS ENTRANT SO FAR: " + "; ".join(bits) if bits else None


def manual_text(booth: bool, day: str) -> str:
    """The static manual: booth RULES with today's admission rule only (<= 3k chars), or the other-screens text.
    Nothing situation-specific: that is now_block(), built per tick from TOD's own answers."""
    if not booth:
        return OTHER_SCREENS
    d = day if day in DAY_RULE_TEXT else "1"
    return BOOTH_MANUAL.format(input=INPUT_LINE, rules=DAY_RULE_TEXT[d])


_CLUTTER_NOW = {"citation": "an M.O.A. citation slip", "flyer": "a flyer (The Pink Vice card)",
                "entry_ticket": "an entry ticket", "rulebook": "the rulebook", "bulletin": "the bulletin"}
_SHELF = "the 'counter shelf left of the desk' target"
_TRAY_CLOSE = "drag the tray tab onto the 'right edge of the desk' target"


def _verdict_now(state: dict, facts: dict) -> str:
    v = tod_verdict(state, facts)
    if not v:
        return "Your verdict: not given yet"
    w = "this tick" if v["where"] == "this frame" else v["where"]
    return f"Your verdict ({w}): {v['value'].upper()} p={v['p']:.2f}"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def now_block(state: dict, day: str, facts: dict | None = None) -> str:
    """'WHAT APPLIES NOW': 1-4 plain sentences for the one situation `situation()` matches, worded from TOD's own
    answers (state + entrant memory). No step letters; no other situation is listed (loop22)."""
    f = facts or {}
    step, _ = situation(state, day, f)
    vside = verdict_side(state, f)
    vtxt = _verdict_now(state, f)
    sc = f.get("stamp_clicks") or []
    out: list[str] = []
    if step.startswith("N"):
        out.append("The person has handed over no documents: there is nothing to stamp. Ask for the passport with "
                   "inspect mode.")
        out.append({"N1": "The rulebook is not open: drag it from its slot onto the 'desk' target (not the shelf).",
                    "N2": "The rulebook is open on another page: click its page corner until Basic Rules shows.",
                    "N3": "The rulebook shows Basic Rules: click the red inspect-mode button.",
                    "N4": "Inspect mode is on: click the rule line 'Entrant must have a passport', then the empty "
                          "counter shelf.",
                    "N5": "An INTERROGATE prompt is visible: click it; the person answers and leaves (or hands over "
                          "a passport)."}[step])
    elif step == "H":
        out.append("Inspect mode is on: papers and stamps do not work. Click the red inspect-mode button to leave it.")
    elif step in ("K", "K1"):
        cl = next(c for c in f.get("clutter") or [] if c["in_way"])
        what = _CLUTTER_NOW.get(cl["id"], cl["id"])
        if cl.get("left_behind"):
            out.append(f"{_cap(what)} was left behind by an entrant who has gone: it cannot be handed back.")
        else:
            where = ("on the " + " and ".join(x.upper() for x in cl["strips"]) + " strip" if cl.get("strips") else
                     "on the passport" if cl.get("on_passport") else "on the desk")
            out.append(f"{_cap(what)} lies {where}; it is not the entrant's document to check -- never stamp it or "
                       "hand it over alone.")
        out.append(f"It lies under the open stamp tray bar: first {_TRAY_CLOSE}, then put it on {_SHELF}."
                   if step == "K1" else f"Drag it onto {_SHELF} first.")
    elif step == "G2":
        out.append("The passport is back and the entrant is still at the window waiting for the rest of their "
                   "papers: drag each paper of theirs still on the desk or counter shelf onto the person.")
    elif step == "G":
        out.append(f"Papers handed back at tick {f.get('handed_back')}: this entrant is finished and leaves by "
                   "themselves; wait.")
    elif step == "A":
        out.append("The window is empty: click the loudspeaker to call the next person; waiting achieves nothing "
                   "(nobody comes until you call).")
    elif step in ("F", "F0", "F2"):
        t_, sd_ = sc[-1]
        out.append(f"You pressed {sd_.upper()} at tick {t_}: the passport counts as stamped; do not press again.")
        if step == "F2":
            out.append(f"{vtxt}: one DENIED press overrules APPROVED; then hand the papers back.")
        elif step == "F0":
            out.append(f"Return the entry ticket (on the {ticket_to_return(state, f)}) to the person first, then the "
                       "passport: drop each ON THE PERSON at the window.")
        else:
            out.append("Hand the papers back: drag the passport ON THE PERSON at the window (any other paper of "
                       "theirs first; the person leaves once the passport is back).")
    elif step == "6":
        out.append("A bulletin or the rulebook covers the passport: drag it aside to the 'desk' target.")
    elif step == "B3":
        out.append("A paper of the entrant's still lies on the counter shelf and has not been read (it may be the "
                   "entry ticket): drag it to the 'desk' target so it can be read.")
    elif step == "E0":
        pu = f.get("passport_under") or []
        bad = [s_ for s_, v in (f.get("strip") or {}).items() if v.get("doc") and v["doc"] not in ("passport", UNREAD)]
        side = vside if vside in bad else bad[0]
        out.append(f"The {f['strip'][side]['doc'].upper()} lies under the {side.upper()} stamp, not the passport (a "
                   "press there is refused): drag it to the 'desk' target to clear that strip.")
        out.append(vtxt + (f"; the passport lies under {' and '.join(x.upper() for x in pu)}." if pu else "."))
    elif step == "E?":
        out.append(f"{vtxt} -- not decided: a reading today's rule needs is missing. Do not stamp; drag the passport "
                   "(or the ticket) to the 'desk' target so it can be read.")
    elif step == "E-":
        pu = " and ".join(x.upper() for x in f.get("passport_under") or [])
        out.append(f"{vtxt}. The passport lies under {pu}: drag it to the strip under the {vside.upper()} stamp.")
    elif step == "E":
        out.append(f"{vtxt}. The passport lies under {vside.upper()}: press the {vside.upper()} stamp once.")
    elif step == "D":
        out.append("The stamp tray is open and the passport is not under a stamp. " + vtxt + "."
                   + (f" Drag the PASSPORT (not the ticket) to the strip under the {vside.upper()} stamp." if vside
                      else " The passport goes under the stamp your verdict names once it is decided."))
    elif step == "D2":
        out.append(f"The stamp tray is open but the passport is not in view (it slid behind the tray): {_TRAY_CLOSE} "
                   "to uncover it.")
    elif step == "B":
        out.append("A paper lies on the counter shelf: drag it onto the 'desk' target so it can be read.")
    elif step == "C":
        if (f.get("desk_target") or {}).get("target") == "desk_clear":
            out.append("The passport's data page is not readable yet: drag the passport onto the 'clear desk space' "
                       "target so the whole page shows.")
        else:
            out.append("The passport is open on the desk and the tray is closed: read it, then open the stamp tray "
                       "(drag the tab at the desk's right edge onto the 'desk' target).")
    else:
        out.append("A person is at the window but none of their papers was seen yet: wait for them to put them down.")
    if f.get("paper_on_passport") and step not in ("K", "K1"):
        out.append(f"A {f['paper_on_passport']['id'].upper()} lies across the open passport: the 'clear desk space "
                   "off the passport' target takes it off.")
    return "WHAT APPLIES NOW (from your own answers above):\n" + "\n".join(f"- {x}" for x in out)


def build(state: dict, history, day: str, ban_lines: list[str] | None = None, facts: dict | None = None) -> str:
    """Request-2 text: the whole manual + what is true now + desk text + last actions. loop21 (run 004042 t115,
    TOD 413 state_too_long at 29k chars): the last HIST_MAX actions, each cut to HIST_LINE_MAX chars; the part
    after the manual is held to STATE_BUDGET chars by dropping the oldest history lines (`facts['text_chars']`,
    `facts['state_chars']`, `facts['hist_kept']` are logged in the tick json)."""
    full_hist = [hist_line(x) for x in list(history)[-HIST_MAX:]]
    booth = (facts or {}).get("booth", True)
    man_t = manual_text(booth, day)
    fixed = [state_block(state, day, facts) + "\n\n" + now_block(state, day, facts) if booth else
             f"- Screen: {state.get('screen', {}).get('value')} (p={state.get('screen', {}).get('p', 0):.2f})",
             desk_text_block(facts, cap=True) if booth else ""]

    def hist_part(hist: list[str]) -> str:
        h = "\n".join(f"- {x}" for x in hist) if hist else "- (none yet; this is the first action)"
        el = entrant_line(facts)
        if el:
            h = f"{short_text(el, 400)}\n{h}"
        head = f"LAST {len(hist)} ACTIONS" if hist else "LAST ACTIONS"
        if (facts or {}).get("cycle_note"):   # the loop's cycle guard (loop.CycleDetector)
            h = f"CYCLE: {short_text(facts['cycle_note'], 400)}\n{h}"
        if (facts or {}).get("tray_note"):    # tray toggle loop: the closing tab is excluded (loop.prepare, audit A5)
            h = f"{short_text(facts['tray_note'], 400)}\n{h}"
        nb = (facts or {}).get("menu_bounces") or 0
        if nb >= 2:
            h = (f"You have gone back and forth between the main menu and day select {nb} times. BACK undoes "
                 f"progress; pick a day tile.\n{h}")
        return f"{head} (oldest first; tick | what was true | input | element | effect):\n{h}"

    hist, bans = full_hist, list(ban_lines or [])
    while True:
        ruled = ("RULED OUT FOR NOW (tried without effect):\n" + "\n".join(f"- {short_text(x, 300)}" for x in bans)
                 if bans else "")
        rest = "\n\n".join(fixed + [hist_part(hist)] + ([ruled] if ruled else []))
        if len(rest) <= STATE_BUDGET or not (hist or len(bans) > 3):
            break
        if hist:
            hist = hist[1:]   # guard: drop the oldest action until the budget holds
        else:
            bans = bans[:-1]  # then ruled-out lines beyond the first 3
    if facts is not None:
        facts["state_chars"] = len(rest)
        facts["text_chars"] = len(man_t) + 2 + len(rest)
        facts["hist_kept"] = len(hist)
    return man_t + "\n\n" + rest


# --------------------------------------------------------------------------
# diagnostics only (never sent to TOD)
# --------------------------------------------------------------------------


CLUTTER_NAMES = {"citation": "an M.O.A. CITATION slip", "flyer": "a flyer (The Pink Vice card)"}


def clutter_phrase(c: dict, tray_open: bool) -> str:
    """State-block tail (a fact, no instruction: now_block says what to do) for a citation slip / flyer."""
    if c.get("left_behind"):
        return " -- LEFT BEHIND by an entrant who has gone"
    who = " -- the inspector's citation slip, not the entrant's" if c["id"] == "citation" else         " -- a flyer, not a document to check"
    if not c["in_way"]:
        return who + "; out of the way"
    if c["under_bar"] and tray_open:
        return who + "; it lies UNDER THE OPEN STAMP TRAY BAR"
    where = (f"on the {' and '.join(x.upper() for x in c['strips'])} strip" if c["strips"]
             else "on the passport" if c["on_passport"] else "on the desk")
    return who + f"; it lies {where}"


def clutter_step(state: dict, f: dict):
    """Manual step K: a citation slip / flyer on the desk (loop20: before every other booth step)."""
    cl = [c for c in f.get("clutter") or [] if c["in_way"]]
    if not cl:
        return None
    tray = yes(state, "stamp_tray_open") or bool(f.get("tray_open_px"))
    under = [c for c in cl if c["under_bar"]]
    if under and tray:
        return "K1", f"close the tray (tab -> right edge) to uncover the {under[0]['id']}"
    return "K", f"drag the {cl[0]['id']} -> counter shelf left of the desk"


def ticket_to_return(state: dict, facts: dict | None) -> str | None:
    """Step F order (loop14): where ('desk' / 'counter shelf') an entry ticket TOD named lies while the person is
    still at the window -- it goes back before the passport. None when there is none."""
    if not yes(state, "person_at_window"):
        return None
    for d in (facts or {}).get("docs_named") or []:
        if d["id"] == "entry_ticket" and d["p"] >= 0.5:
            return "counter shelf" if d["where"] == "counter" else "desk"
    return None


def no_passport(state: dict, facts: dict | None = None) -> bool:
    """Step N: TOD (request 1) says the person at the window has handed over no documents (p >= NO_DOCS_P), and
    TOD's other answers agree: nothing on the counter shelf, no paper TOD named the passport (run 092642 t9:
    no_documents 0.66 with counter 0.64 and the passport on the counter -> step B, not N). Loop22 (run 021346
    t30-55): a paper TOD's identity answer leaves UNREAD (or names a ticket) on the desk / counter is not "no
    documents" either -- it is read first (no_documents 0.62-0.66 with the passport back on the shelf, unread)."""
    if (not yes(state, "no_documents_presented", NO_DOCS_P) or not yes(state, "person_at_window")
            or yes(state, "document_on_counter_shelf")):
        return False
    return not any((d["id"] == "passport" and d["p"] >= 0.5) or d["id"] in ("unread", "entry_ticket")
                   for d in (facts or {}).get("docs_named") or [])


def situation(state: dict, day: str = "1", facts: dict | None = None) -> tuple[str, str]:
    """(step letter, intended input) per manual section 4 -- for logs and the
    dry-run pass/fail check only."""
    scr = state.get("screen", {}).get("value", "")
    if scr and scr not in ("booth_idle", "documents_on_desk", "stamp_tray_open", "inspect_mode"):
        return "7", f"non-booth screen ({scr}): click to continue"
    if no_passport(state, facts):
        rp = (state.get("rulebook_page") or {}).get("value", "not_open")
        if yes(state, "interrogate_prompt_visible"):
            return "N5", "click the interrogate prompt"
        if yes(state, "inspect_mode_on"):
            return "N4", "inspect mode: click the passport rule line, then the empty counter shelf"
        if rp == "basic_rules":
            return "N3", "click the inspect-mode button"
        if rp == "not_open":
            return "N1", "drag the rulebook from its slot onto the desk"
        return "N2", "click the rulebook page corner until BASIC RULES shows"
    if yes(state, "inspect_mode_on"):
        return "H", "click the inspect-mode button (leave inspect mode)"
    k_step = clutter_step(state, facts or {})   # loop20: clutter is stowed before B-G (user priority)
    if k_step:
        return k_step
    if (facts or {}).get("waiting_docs") and yes(state, "person_at_window"):
        return "G2", "drag the remaining document (entry ticket) -> entrant"
    if (facts or {}).get("handed_back") is not None:
        return ("G", "wait for the entrant to leave") if yes(state, "person_at_window") else ("A", "click loudspeaker")
    kc = known_country(state, facts)
    if kc and kc[0] == "unreadable":
        kc = None
    vside = verdict_side(state, facts)   # TOD's own verdict answer (diagnostic: which step TOD's answer implies)
    if stamped(state, facts):
        if pressed_side(facts) == "approved" and vside == "denied":
            return "F2", "click DENIED (overrules APPROVED)"
        if ticket_to_return(state, facts):
            return "F0", "drag the entry ticket -> entrant (before the passport)"
        return "F", "drag stamped passport -> entrant (hand back)"
    if yes(state, "bulletin_or_rulebook_covering_desk") and yes(state, "document_open_on_desk"):
        return "6", "drag bulletin/rulebook -> desk (aside)"
    f = facts or {}
    # loop13 run 182519 t89-92 (Maslov): the ticket stayed on the counter. Any counter paper that is UNREAD (identity
    # gate) or TOD's entry ticket goes to the desk once the passport is open there (no tick cap: the identity gate
    # keeps a stowed slip from looping it, it is named on the desk)
    if (yes(state, "document_open_on_desk") and any(
            d["where"] == "counter" and (d["id"] == "entry_ticket" or (d["id"] == UNREAD and not f.get("stowed")))
            for d in f.get("docs_named") or [])):
        return "B3", "drag the unread / ticket paper on the counter -> desk so it can be read"
    if yes(state, "stamp_tray_open") or f.get("tray_open_px"):
        wrong = [s_ for s_, v in (f.get("strip") or {}).items() if v.get("doc") and v["doc"] not in ("passport", UNREAD)]
        # ... also when the passport lies under the other head and the paper is on TOD's verdict side (run 232544
        # t25-29: ticket under APPROVED, passport under DENIED, verdict approved -> 'drag passport under APPROVED'
        # could not be done, TOD pressed DENIED x5, refused)
        if wrong and (not f.get("passport_under") or vside in wrong):
            wrong = [vside] if vside in wrong else wrong
            return "E0", f"drag the {f['strip'][wrong[0]]['doc']} off the {wrong[0].upper()} strip -> desk"
        if f.get("passport_under"):
            if vside is None:
                return "E?", "TOD's verdict: cannot_decide_yet / not given: drag passport -> desk to read it"
            pu = f.get("passport_under") or []
            if vside not in pu:
                return "E-", f"drag passport -> strip under the {vside.upper()} head (TOD's verdict)"
            return "E", "click " + vside.upper()
        if yes(state, "document_open_on_desk"):
            return "D", "drag passport -> stamp landing strip"
        if (yes(state, "person_at_window") and (facts or {}).get("tray_flips", 0) < TRAY_FLIP_LIMIT
                and not yes(state, "document_on_counter_shelf")):
            return "D2", "drag tray tab -> right edge (close tray, reveal hidden passport)"
    if ((yes(state, "document_on_counter_shelf") or any(
            d["where"] == "counter" and d["id"] in ("passport", "entry_ticket", UNREAD) and not f.get("stowed")
            for d in f.get("docs_named") or []))
            and not yes(state, "document_open_on_desk")):
        # loop22 (021346 t30): counter 0.27 'no' but TOD's identity answer names a paper on the counter (unread)
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
             "passport booklet", "paper document", "ticket", "entry ticket", "flyer (The Pink Vice)",
             "bulletin board", "tab at screen edge",
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


STAMP_CAPS = {"rubber stamp", "red rubber stamp", "green rubber stamp"}


def affordance_text(box, cls: str | None) -> str:
    """The element's input as a screen fact for its option text (B26): what the element IS operated by, derived
    from its class (`cls` = loop._cls / input_class). It describes the element, it does not say which to pick.
    '' when the class is unknown or the element is a drop target."""
    if cls == "click":
        name, cap = getattr(box, "name", "") or "", getattr(box, "caption", "") or ""
        if name.startswith("stamp_") or cap in STAMP_CAPS:
            return "click (press to stamp)"
        if getattr(box, "kind", "") == "page_corner":
            return "click (turns the page)"
        return "click"
    if cls == "drag":
        return "drag"
    return ""
