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
         "issuing country is ARSTOTZKA, otherwise DENIED. Expiry date and photo are NOT checked on Day 1 "
         "(those checks start on Day 2).",
    "2": "Day 2 (1982.11.24): passport only. Foreigners may now enter too. APPROVED if the passport is not "
         "expired (expiry after 1982.11.24), the photo matches the person and the ISS. city belongs to the passport's "
         "country; otherwise DENIED.",
    "3": "Day 3 (1982.11.25): Arstotzkan citizens need only a valid passport. Foreigners need a valid passport "
         "AND an entry ticket dated 1982.11.25; a missing ticket or a ticket with any other date -> DENIED.",
}

# --------------------------------------------------------------------------
# the manual (static text, sent in full every tick)
# --------------------------------------------------------------------------

MANUAL = """\
PAPERS, PLEASE -- HOW TO WORK THE BORDER BOOTH WITH THE MOUSE

You are the border inspector. Every input is either a CLICK on one numbered element or a DRAG of one
numbered element onto a numbered drop target. You decide one input per turn by choosing the element; whether
it is clicked or dragged follows from the element (section 2). Choose "wait" when nothing should be done.

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
- "counter shelf left of the desk -- drop the rulebook, bulletin or a flyer here to put it away": an open rulebook or
  bulletin dropped here closes and leaves the desk. Neither is needed on Days 1-3.

4. PROCESSING ONE ENTRANT -- FIND THE FIRST LINE THAT MATCHES WHAT IS CURRENTLY TRUE
A. Nobody is at the window and no document is on the counter or desk: click the loudspeaker on the booth
   roof to call the next person. The loudspeaker ONLY works when the window is empty and the desk is clear;
   while a person stands at the window it does nothing (it is then not even offered). The person then walks up to the window by themselves; if someone is
   already walking up, wait.
B. A person is at the window and their passport lies on the counter shelf under the window: drag the
   passport down to the desk ("desk" target) to open it. Clicking it does nothing. From Day 3 a foreigner
   also hands over an ENTRY TICKET (small slip): drag it to the desk as well so its date can be read.
C. An open passport lies on the desk and the stamp tray is closed: read the passport (on Day 1 only the
   issuing country at the bottom matters; from Day 2 also the EXP. date and photo), then open the stamp tray by dragging the tab at the right edge of the
   desk to the left (drop it on the "desk" target). If the state block says the passport's data page is NOT
   readable (half hidden or clipped), the verdict cannot be decided yet: first drag the passport onto the
   "clear desk space" target so its whole page shows.
D. The stamp tray is open but the passport is NOT lying under a stamp head (it is off to the side, or it
   has slid up behind the tray so only an edge shows): stamps only mark a document lying directly beneath
   the stamp heads, in the dark strip under the tray. First decide APPROVED or DENIED (section 5), then
   drag the PASSPORT (not the entry ticket) to the stamp landing strip under THAT stamp. The verdict cannot be
   decided while the passport's data page is not readable: if the state block says "Passport data page
   readable: no", the next move is the passport onto the "clear desk space" target, NOT onto a stamp strip. Both strips are valid landing places:
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
   will be refused. Drag the rulebook or bulletin onto the "counter shelf left of the desk" target (other
   papers to the "desk" target), then drag the PASSPORT (from the
   counter shelf or the desk) to the strip under the stamp you need. The rulebook is never needed on Day 1.
E. The stamp tray is open, the passport lies under a stamp head (the state block says "The passport is
   under: APPROVED" or "DENIED"), and it is NOT stamped yet (none of the three "stamped" signs of F is in the
   state block): decide with section 5, then click the stamp the passport is lying under; if you want the
   other decision, first drag the passport to the other strip. ONE click. Clicking the stamp the passport is
   NOT under stamps nothing. Decide only when the issuing country is known -- the state block shows it either as read in this
   frame or as "passport read as <COUNTRY> at tick N". If the country is not known yet (the bottom of the
   passport with the country name is not visible), do not stamp: drag the passport to the "desk" target so
   the whole page can be read, then put it under the stamp you need.
E?. The state block says "The decision is not known yet": the stamps are not offered. Drag the passport
   to the "desk" target so its data page can be read; it goes under a stamp after that.
E-. The state block says "the passport is under the wrong stamp": the stamps are not offered this turn (a
   press would mark nothing). Drag the passport onto the landing strip the state block names.
H. INSPECT MODE (the state block says "Inspect mode is ON": desk darkened, red dotted frame, red text
   HIGHLIGHT DISCREPANCIES): documents cannot be moved and stamps cannot be used while it is on. Click the red
   inspect-mode button at the lower right of the desk once to leave it, then continue with the matching step.
   Inspect mode is not needed on Days 1-3 except in step N (no passport presented): the button is only offered
   while inspect mode is on or in step N, and in step N you stay in inspect mode until the interrogation is done.
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
N. NO DOCUMENTS: the state block says "The person at the window has handed over no documents: yes". There is
   nothing to stamp: this entrant is sent away WITHOUT a stamp; the stamp tray and the stamps are not offered.
   Waiting does not help (the day does not go on until you ask for the passport). Ask for it with inspect
   mode, one input per tick, reading the state block like in C/D/E:
   N1. "Rulebook page open on the desk: NOT_OPEN": drag the rulebook from its slot below the counter onto the
       DESK (target "desk (drop documents here to read them)"). Not onto the counter shelf left of the desk: that
       puts the rulebook away again.
   N2. Rulebook open on another page: click its page corner until the page is BASIC_RULES.
   N3. Rulebook on BASIC_RULES, inspect mode off: click the red inspect-mode button.
   N4. Inspect mode ON, no interrogate prompt: click the rule line "Entrant must have a passport", then click
       the EMPTY counter shelf.
   N5. "An INTERROGATE prompt is visible: yes": click it. The entrant answers and leaves on their own (or
       hands over a passport -- then continue with B). Then go back to A.
G2. The state block says the entrant is STILL at the window waiting for the rest of their documents: drag
   each paper of theirs still on the desk or the counter shelf (entry ticket ...) onto the entrant.
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
  expired (expiry after 1982.11.24), the photo matches the person and the ISS. (issuing) city belongs to the
  passport's country; otherwise DENIED. Valid issuing cities (rulebook Regional Map): ARSTOTZKA: Orvech
  Vonor, East Grestin, Paradizna; ANTEGRIA: St. Marmero, Glorian, Outer Grouse; IMPOR: Enkyo, Haihan,
  Tsunkeido; KOLECHIA: Yurko City, Vedor, West Grestin; OBRISTAN: Skal, Lorndaz, Mergerous; REPUBLIA: True
  Glorian, Lesrenadi, Bostan; UNITED FEDERATION: Great Rapid, Shingleton, Korista City.
- Day 3, 1982.11.25: Arstotzkans need a valid passport only. Foreigners also need an entry ticket dated
  1982.11.25; no ticket or a different date -> DENIED.
The first entrant of day 1 is the tutorial; follow the same rule (his passport is Arstotzkan -> APPROVED).

6. BULLETIN, RULEBOOK AND MULTI-PAGE PAPERS
- The bulletin (Ministry of Admission sheet) and the rulebook can lie open on the desk. If one covers the
  passport or the place you need to work, drag it aside to the left part of the desk ("desk" target). They
  are not needed to process day-1 entrants.
- An M.O.A. CITATION slip (printed after a mistake) is the inspector's, not the entrant's: never hand it to the
  entrant; drag it to the "counter shelf left of the desk" target if it is in the way.
- A loose flyer or note an entrant leaves (e.g. the pink "The Pink Vice" card) is not needed: if it lies on the
  passport or under a stamp, DRAG it (clicking it does nothing) onto the "counter shelf left of the desk" target.
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
- End of day (the family budget screen: SAVINGS, SALARY, RENT, HEAT, FOOD, MEDICINE ..., a total, SLEEP): the
  total at the bottom is your money after tonight. If it would be NEGATIVE (a minus sign, e.g. "$-5") you are
  arrested for debt and the game is over. Click HEAT, FOOD (and MEDICINE) one at a time to untick them until
  the total is zero or more, then click SLEEP. RENT cannot be unticked. If the total is not negative, click
  SLEEP right away.
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
INSPECT_KEYS = ("issuing_country", "photo_matches_person")
CHECK_KEYS = ("entry_ticket_dated_today", "photo_matches_person")
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
            "passport or other document lie on that shelf?",
            "yes - a passport/document lies on the counter shelf under the window",
            "no - the counter shelf is empty"),
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


def doc_question(d: dict) -> dict:
    """Request-1 identity question for one paper the layout found (position + the OCR text inside it)."""
    where = "on the counter shelf under the booth window" if d["where"] == "counter" else "on the dark desk"
    txt = "; ".join(repr(t) for t in d.get("text") or []) or "(no readable text)"
    return {"type": "choice",
            "instructions": f"Look at the paper lying {where}, at the {d['pos']} of the picture. OCR read inside "
                            f"it: {txt}. What is this paper? Text such as ENTRY VISA, a name, DOB, SEX, ISS. or EXP. "
                            f"means the open passport; ENTRY TICKET or VALID ON (a small slip, often with ARSTOTZKA "
                            f"as its header) means the entry ticket.",
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


NO_DOCS_HINT_RE = re.compile(r"NO DOCUMENTS|INSPECT mode|to interrogate", re.I)


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
    hint = [t for t in lines if NO_DOCS_HINT_RE.search(t)]
    if hint:   # the game's own slip: 'THIS ENTRANT HAS NO DOCUMENTS / To proceed, use INSPECT mode to interrogate'
        out.append("GAME HINT ON SCREEN (OCR): " + " / ".join(hint))
    return "\n".join(out)


def stamped(state: dict, facts: dict | None) -> list[str]:
    """The three 'passport is stamped' signs that are currently true (manual rule F)."""
    facts = facts or {}
    out = []
    if yes(state, "passport_shows_stamp_mark") and (facts.get("stamp_clicks") or facts.get("missed_stamps")):
        # a mark needs a stamp press for this entrant (run 054238 t62-137: mark p=0.77 on an unstamped passport)
        out.append("mark")
    if facts.get("stamp_clicks"):
        out.append("history")
    return out


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
    """True/False/None from one request-1 answer of a check (3-way choice or yes/no)."""
    if not a:
        return None
    if isinstance(a.get("value"), str) and a["value"] not in ("True", "False"):
        v = CHECK_YES.get(a["value"])
        # a finding that denies needs more confidence (run 022439 t60: a valid passport read 'expired' at 0.61)
        return v if v is not None and a["p"] >= (0.6 if v else DENY_P) else None
    return (a["p"] >= 0.5) if abs(a["p"] - 0.5) >= CARRY_CHECK_MARGIN else None


def known_exp(state: dict, facts: dict | None):
    """(YYYY.MM.DD, p, where) of the EXP. date TOD picked among the OCR dates (this frame, else carried), or None."""
    e = state.get("exp_read")
    if e and e["p"] >= 0.5:
        return e["value"], e["p"], "this frame"
    c = (facts or {}).get("exp_carried")
    return (c["value"], c["p"], f"tick {c['tick']}") if c else None


def expiry_valid(state: dict, day: str, facts: dict | None):
    """True/False/None: the EXP. date TOD picked, compared with today (section 5). 'Expired' needs p >= DENY_P."""
    e = known_exp(state, facts)
    if not e:
        return None
    y, m, d = (int(x) for x in e[0].split("."))
    ok = (y, m, d) > tuple(int(x) for x in DAY_DATES.get(day, DAY_DATES["2"]).split("."))
    return ok if ok or e[1] >= DENY_P else None


# ---- request 1b: Day 2/3 readings as choices over the strings the OCR read on the screen ----------------------
# (private eval 2026-10-03, 30 gt-labelled Day 2 frames: expiry 3/30 -> 28/30 right, 0 wrong; city 26/30 -> 30/30)
_DATE_RE = re.compile(r"(19\d\d)[.,](\d\d)[.,](\d\d)")
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
    q, cand = {}, {"dates": ocr_dates(desk_text), "toks": ocr_city_tokens(desk_text)}
    if cand["dates"]:
        q["exp_date"] = {
            "type": "choice",
            "instructions": "OCR found these dates on the documents on the desk. On the open passport data page, "
                            "which one is printed after 'EXP.' (the expiry date; the date after 'DOB.' is the birth "
                            "date)?",
            "criteria": {**{f"D{i + 1}": f"EXP. {d}" for i, d in enumerate(cand["dates"])},
                         "none": "none of these is the passport's EXP. date, or no open passport data page is visible"}}
    if day == "3" and cand["dates"]:
        # run 092642 Troyer: ticket VALID ON 1982.12.09 read 'dated_today' by the yes/no-style question
        q["ticket_date"] = {
            "type": "choice",
            "instructions": "OCR found these dates on the documents on the desk. On the ENTRY TICKET (small slip with "
                            "'ENTRY TICKET' and 'VALID ON'), which one is printed after 'VALID ON'?",
            "criteria": {**{f"D{i + 1}": f"VALID ON {d}" for i, d in enumerate(cand["dates"])},
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
    i = _pick(state.get("ticket_date"), len(cand["dates"]))
    if i is not None:   # the ticket's date as picked among the OCR dates overrides the direct ticket answer
        same = cand["dates"][i] == DAY_DATES["3"]
        state["entry_ticket_dated_today"] = {"value": "dated_today" if same else "other_date",
                                             "p": state["ticket_date"]["p"], "from": f"VALID ON {cand['dates'][i]}"}
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


def check_value(state: dict, facts: dict | None, k: str):
    """True/False for a yes/no inspection check: this frame's answer, else the entrant's carried reading, else None.
    issuing_city_valid = the rule table (section 5) applied to TOD's city + country readings."""
    if k == "expiry_after_today":
        return expiry_valid(state, (facts or {}).get("day", "2"), facts)
    if k == "issuing_city_valid":
        kc, ci = known_country(state, facts), known_city(state, facts)
        if not kc or not ci:
            return None
        return ci[0] in ISSUING_CITIES.get(kc[0], ())
    v = check_answer(state.get(k))   # run 022439 t74: expiry p=0.44 is not a 'no'
    if v is not None:
        return v
    c = ((facts or {}).get("checks_carried") or {}).get(k)
    return c["value"] if c else None


def needed_stamp(state: dict, day: str, facts: dict | None) -> str | None:
    """'approved'/'denied' per section 5 from TOD's request-1 answers (read now or carried for this entrant), or
    None while a check the day needs is still unknown."""
    kc = known_country(state, facts)
    if not kc or kc[0] == "unreadable":
        return None
    if day not in ("2", "3"):
        return "approved" if kc[0] == "ARSTOTZKA" else "denied"
    keys = ["expiry_after_today", "issuing_city_valid", "photo_matches_person"]
    if day == "3" and kc[0] != "ARSTOTZKA":
        keys.append("entry_ticket_dated_today")
    vals = [check_value(state, {**(facts or {}), "day": day}, k) for k in keys]
    if vals[1] is None:
        vals[1] = True   # city not read confidently: only a confident mismatch denies (an unread city must not stall)
    if vals[2] is None:
        vals[2] = True   # photo: only a confident 'different' denies
    if len(vals) > 3 and vals[3] is None:
        vals[3] = True   # Day 3 ticket: only a confident 'other date' denies (an unread ticket must not stall)
    if False in vals:
        return "denied"
    if None in vals:
        return None
    return "approved"


def undecided_stamp(state: dict, day: str, facts: dict | None) -> bool:
    """Tray open, passport under a stamp head, not stamped, but section 5 cannot be decided yet (manual step E?):
    loop.prepare leaves the stamps out of the options (run 021438 t43: APPROVED pressed with the country unknown,
    correct verdict was DENIED)."""
    facts = facts or {}
    if not (yes(state, "stamp_tray_open") or facts.get("tray_open_px")) or stamped(state, facts):
        return False
    return bool(facts.get("passport_under")) and needed_stamp(state, day, facts) is None


def wrong_stamp(state: dict, day: str, facts: dict | None):
    """(side the passport lies under, side it needs) when the tray is open, request 1 puts the passport under
    exactly one stamp head, the passport is not stamped yet and section 5 needs the OTHER stamp (manual step E-);
    else None. loop.prepare then leaves the stamps out of the options: a press there is refused anyway."""
    facts = facts or {}
    if not (yes(state, "stamp_tray_open") or facts.get("tray_open_px")) or stamped(state, facts):
        return None
    pu = facts.get("passport_under") or []
    need = needed_stamp(state, day, facts)
    if len(pu) == 1 and need and need != pu[0]:
        return pu[0], need
    return None


def state_block(state: dict, day: str, facts: dict | None = None) -> str:
    facts = facts or {}
    today = DAY_DATES.get(day, DAY_DATES["1"])
    lines = ["WHAT IS CURRENTLY TRUE ON SCREEN (read from this frame by a separate check; p = confidence):"]
    scr = state.get("screen")
    if scr:
        lines.append(f"- Screen: {scr['value']} (p={scr['p']:.2f})")
    for k in STATE_KEYS:
        if k == "passport_shows_stamp_mark" and "mark" not in stamped(state, facts):
            if facts.get("stamp_clicks"):   # run 070005 t25-31: said 'no stamp pressed' after 3 recorded presses
                t_, side_ = facts["stamp_clicks"][-1]
                lines.append(f"- {_LABEL[k]}: the passport was stamped {side_.upper()} at tick {t_} (the ink may be "
                             f"hidden under the tray in this frame)")
            else:
                lines.append(f"- {_LABEL[k]}: no (no stamp has been pressed for this entrant yet)")
            continue
        lines.append(f"- {_LABEL[k]}: {_yn(state, k)}")
    for k in ("no_documents_presented", "interrogate_prompt_visible"):
        if k in state:
            lines.append(f"- {_LABEL[k]}: {_yn(state, k)}")
    rp = state.get("rulebook_page")
    if rp:
        lines.append(f"- Rulebook page open on the desk: {rp['value'].upper()} (p={rp['p']:.2f})")
    if no_passport(state, facts):
        lines.append("- The person has presented no documents: there is nothing to stamp; they are sent away "
                     "without a stamp. Ask for the passport with inspect mode (step N)")
        if (state.get("rulebook_page") or {}).get("value", "not_open") == "not_open":
            lines.append("- Step N1: the rulebook goes from its slot below the counter onto the DESK (target 'desk "
                         "(drop documents here to read them)'), not onto the counter shelf left of the desk")
    for d in facts.get("docs_named") or []:
        where = "counter shelf" if d["where"] == "counter" else "desk"
        tail = (" -- not needed on Days 1-3; to clear the desk drop it on the 'counter shelf left of the desk' target"
                if d["id"] in ("rulebook", "bulletin") and d["where"] == "desk" and d["p"] >= 0.6
                and not (d["id"] == "rulebook" and no_passport(state, facts)) else "")   # step N reads it on the desk
        lines.append(f"- Paper on the {where} ({d['pos']}): {d['id'].upper()} (p={d['p']:.2f}){tail}")
    if (facts.get("desk_target") or {}).get("target") == "desk_clear":
        # loop.passport_needs_clear_space: an open passport on the desk whose data page request 1/1b could not read
        c = state.get("issuing_country") or {}
        why = [f"issuing country read as {c.get('value', 'unknown')} (p={c.get('p', 0):.2f})"]
        if day in ("2", "3") and not known_exp(state, facts):
            why.append("EXP. date not read")
        lines.append("- Passport data page readable: no (" + "; ".join(why) + "). The verdict cannot be decided "
                     "until the page is readable: drag the passport onto the 'clear desk space' target, not onto a "
                     "stamp strip")
    if yes(state, "passport_open_readable") and not yes(state, "stamp_tray_open"):
        lines.append("- The passport is already open on the desk and the stamp tray is closed: dragging the passport "
                     "to the desk again changes nothing; the next step is C (drag the stamp tray tab to the desk)")
    for t, side in facts.get("stamp_clicks") or []:
        lines.append(f"- A stamp was clicked at tick {t} ({side.upper()}) and the screen changed: the passport is "
                     f"stamped {side.upper()}")
    for t, side in facts.get("missed_stamps") or []:
        lines.append(f"- The {side.upper()} stamp was clicked at tick {t} while the passport lay under the other "
                     "stamp: nothing was stamped")
    if (facts.get("tray_flips") or 0) >= TRAY_FLIP_LIMIT:
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
                         "strip (the rulebook/bulletin onto the 'counter shelf left of the desk' target, other papers to the "
                         "'desk' target), then drag the PASSPORT to the strip under the stamp you need")
        ws = wrong_stamp(state, day, facts)
        need = needed_stamp(state, day, facts)
        if need and not stamped(state, facts):
            kc = known_country(state, facts)
            why = f"issuing country {kc[0]}" if day not in ("2", "3") else "the passport readings in this list"
            lines.append(f"- Section 5 applied to {why} -> {need.upper()}: the passport belongs in the landing "
                         f"strip under the {need.upper()} stamp")
        if ws:
            lines.append(f"- The passport is under the wrong stamp: drag it onto the {ws[1].upper()} landing strip")
        elif undecided_stamp(state, day, facts):
            lines.append("- The decision is not known yet (a check of section 5 could not be read): the stamps are not "
                         "offered. Drag the passport to the 'desk' target so its data page can be read"
                         + ("" if yes(state, "document_open_on_desk") else
                            "; if no open passport is visible on the desk (it may be hidden under the open stamp "
                            "tray), first drag the stamp tray tab right to put the tray away"))
        elif stamped(state, facts):
            lines.append("- The passport is already stamped: do not press a stamp again; hand the passport back "
                         "(drag it onto the entrant at the window)")
        elif len(pu) == 1:
            other = "approved" if pu[0] == "denied" else "denied"
            lines.append(f"- Click the stamp the passport is lying under ({pu[0].upper()}); if you want "
                         f"{other.upper()} instead, first drag the passport to the strip under the {other.upper()} "
                         f"stamp. Clicking {other.upper()} now would stamp nothing.")
        elif not pu and (facts.get("desk_target") or {}).get("target") == "desk_clear":
            lines.append("- The passport lies under neither stamp and its page is not readable yet: the stamps are "
                         "not offered; move the passport onto the 'clear desk space' target first")
        elif not pu:
            lines.append("- The passport lies under neither stamp: the stamps are not offered (a press would mark "
                         "nothing); drag the passport to the landing strip under the stamp you want first")
    if facts.get("waiting_docs"):
        hb = f" at tick {facts['handed_back']}" if facts.get("handed_back") is not None else " (no passport is visible)"
        lines.append(f"- The passport was handed back{hb}, but the entrant is STILL at the "
                     "window: they wait for the rest of their documents (e.g. the entry ticket). Drag every paper "
                     "of theirs still lying on the desk or the counter shelf onto the entrant (step G2)")
    elif facts.get("handed_back") is not None:
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
    if day in ("2", "3"):
        e = known_exp(state, facts)
        if e:
            ok_e = expiry_valid(state, day, facts)
            lines.append(f"- Passport EXP. read as {e[0]} (p={e[1]:.2f}, {e[2]}); today is {today}: "
                         + ("not expired" if ok_e else "EXPIRED" if ok_e is False else "not decidable yet"))
    for k, c in ((facts.get("checks_carried") or {}).items() if day in ("2", "3") else ()):
        if k not in state:
            lines.append(f"- {_CHECK_LABEL[k]}: {'yes' if c['value'] else 'no'} (read at tick {c['tick']}, p={c['p']:.2f})")
    ci = known_city(state, facts)
    if day in ("2", "3") and ci:
        kc = known_country(state, facts)
        ok_c = check_value(state, facts, "issuing_city_valid")
        lines.append(f"- Passport ISS. city: {ci[0]} (p={ci[1]:.2f}, {ci[2]})" + (
            "" if ok_c is None else f" -- {'a valid' if ok_c else 'NOT a valid'} issuing city of {kc[0]}"))
    if day == "3" and "entry_ticket_dated_today" in state:
        lines.append(f"- An entry ticket dated today ({today}) is visible: {_yn(state, 'entry_ticket_dated_today')}")
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


def _sections() -> dict:
    import re
    parts = re.split(r"\n(?=\d\. )", MANUAL)
    out = {0: parts[0]}
    for p in parts[1:]:
        out[int(p.split(".", 1)[0])] = p
    return out


def _day_rule_section(sec5: str, day: str) -> str:
    """Section 5 with only today's bullet (the other days' rules are not in force)."""
    d = day if day in DAY_RULES else "1"
    lines = sec5.rstrip("\n").split("\n")
    head, keep, cur = [lines[0]], [], None
    for ln in lines[1:]:
        if ln.startswith("- Day "):
            cur = ln[6]
        elif not ln.startswith("  "):
            cur = "tail1"
        if cur == d or (cur == "tail1" and d == "1"):
            keep.append(ln)
    return "\n".join(head + keep) + "\n"


def manual_text(booth: bool, day: str) -> str:
    """The manual sections for this screen family (run 054238: 3.8k-word request 2 = 5.5 s per TOD call;
    measured 2.3 s at 2k words, 1.3 s at 1k). Booth: sections 1-6 and 8 with today's rule only; other screens:
    section 7. No step is pre-selected inside a section."""
    S = _sections()
    if not booth:
        return S[0] + "\n" + S[7]
    return "\n".join([S[0], S[1], S[2], S[3], S[4], _day_rule_section(S[5], day), S[6], S[8]])


def build(state: dict, history, day: str, ban_lines: list[str] | None = None, facts: dict | None = None) -> str:
    """Request-2 text: the whole manual + what is true now + desk text + last actions."""
    hist = list(history)
    h = "\n".join(f"- {x}" for x in hist) if hist else "- (none yet; this is the first action)"
    el = entrant_line(facts)
    if el:
        h = f"{el}\n{h}"
    head = f"LAST {len(hist)} ACTIONS" if hist else "LAST ACTIONS"
    if (facts or {}).get("cycle_note"):   # the loop's cycle guard (loop.CycleDetector)
        h = f"CYCLE: {facts['cycle_note']}\n{h}"
    nb = (facts or {}).get("menu_bounces") or 0
    if nb >= 2:
        h = (f"You have gone back and forth between the main menu and day select {nb} times. BACK undoes "
             f"progress; pick a day tile.\n{h}")
    booth = (facts or {}).get("booth", True)
    parts = [manual_text(booth, day), state_block(state, day, facts) if booth else
             f"- Screen: {state.get('screen', {}).get('value')} (p={state.get('screen', {}).get('p', 0):.2f})",
             desk_text_block(facts) if booth else "",
             f"{head} (oldest first; tick | what was true | input | element | effect):\n{h}"]
    if ban_lines:
        parts.append("RULED OUT FOR NOW (tried without effect):\n" + "\n".join(f"- {s}" for s in ban_lines))
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# diagnostics only (never sent to TOD)
# --------------------------------------------------------------------------


def no_passport(state: dict, facts: dict | None = None) -> bool:
    """Step N: TOD (request 1) says the person at the window has handed over no documents (p >= NO_DOCS_P), and
    TOD's other answers agree: nothing on the counter shelf, no paper TOD named the passport (run 092642 t9:
    no_documents 0.66 with counter 0.64 and the passport on the counter -> step B, not N)."""
    if (not yes(state, "no_documents_presented", NO_DOCS_P) or not yes(state, "person_at_window")
            or yes(state, "document_on_counter_shelf")):
        return False
    return not any(d["id"] == "passport" and d["p"] >= 0.5 for d in (facts or {}).get("docs_named") or [])


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
    if (facts or {}).get("waiting_docs") and yes(state, "person_at_window"):
        return "G2", "drag the remaining document (entry ticket) -> entrant"
    if (facts or {}).get("handed_back") is not None:
        return ("G", "wait for the entrant to leave") if yes(state, "person_at_window") else ("A", "click loudspeaker")
    kc = known_country(state, facts)
    if kc and kc[0] == "unreadable":
        kc = None
    ok = bool(kc) and kc[0] == "ARSTOTZKA"   # Day 1: the only rule
    if day in ("2", "3"):
        ok = needed_stamp(state, day, facts) == "approved"
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
            if not kc or needed_stamp(state, day, facts) is None:
                return "E?", "decision unknown: drag passport -> desk to read it"
            need = "approved" if ok else "denied"
            pu = (facts or {}).get("passport_under") or []
            if pu and need not in pu:
                return "E-", f"drag passport -> strip under the {need.upper()} head (it lies under {pu[0].upper()})"
            return "E", "click " + need.upper()
        if yes(state, "document_open_on_desk"):
            return "D", "drag passport -> stamp landing strip"
        if (yes(state, "person_at_window") and (facts or {}).get("tray_flips", 0) < TRAY_FLIP_LIMIT
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
