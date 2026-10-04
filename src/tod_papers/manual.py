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

The verdict (APPROVED / DENIED / cannot_decide_yet) is TOD's own answer to
`verdict_question` (request 1b): today's rule as text plus TOD's own earlier
readings of the entrant's papers, labelled as such. No code computes a verdict
(the old needed_stamp / wrong_stamp / undecided_stamp were removed, audit A1-A4).

`situation(state)` names the manual step that applies to a state. It is for
logs and dry-run evaluation only and is never sent to TOD.

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

MANUAL = """\
PAPERS, PLEASE -- HOW TO WORK THE BORDER BOOTH WITH THE MOUSE

You are the border inspector. Every input is either a CLICK on one numbered element or a DRAG of one
numbered element onto a numbered drop target. Each turn you choose the input (click, drag or wait), the element
and, for a drag, the drop target. Choose "wait" when nothing should be done.

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

2. CLICK OR DRAG
""" + INPUT_LINE + """ (Each numbered element's text ends with its input: "— click" or "— drag".)

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
- "counter shelf left of the desk -- drop the rulebook, bulletin, a flyer or a citation slip here to put it away": an
  open rulebook or bulletin dropped here closes and leaves the desk; a citation slip or flyer dropped here is out of
  the way. None of them is needed on Days 1-3. Offered whenever such a paper is on the desk.

4. PROCESSING ONE ENTRANT -- FIND THE FIRST LINE THAT MATCHES WHAT IS CURRENTLY TRUE
A. Nobody is at the window and no document is on the counter or desk: click the loudspeaker on the booth
   roof to call the next person. The loudspeaker ONLY works when the window is empty and the desk is clear;
   while a person stands at the window it does nothing (it is then not even offered). The person then walks up to the window by themselves; if someone is
   already walking up, wait.
B. A person is at the window and their passport lies on the counter shelf under the window: drag the
   passport down to the desk ("desk" target) to open it. Clicking it does nothing. From Day 3 a foreigner
   also hands over an ENTRY TICKET (small slip): drag it to the desk as well so its date can be read.
   A paper the state block calls UNREAD ("document on the counter -- unread": nobody could tell what it is,
   counter papers are too small to read) is not known to be a ticket, a flyer or anything else: drag it to the
   "desk" target so it can be read.
C. An open passport lies on the desk and the stamp tray is closed: read the passport (on Day 1 only the
   issuing country at the bottom matters; from Day 2 also the EXP. date and ISS. city), then open the stamp tray by dragging the tab at the right edge of the
   desk to the left (drop it on the "desk" target). If the state block says the passport's data page is NOT
   readable (half hidden or clipped), the verdict cannot be decided yet: first drag the passport onto the
   "clear desk space" target so its whole page shows.
D. The stamp tray is open but the passport is NOT lying under a stamp head (it is off to the side, or it
   has slid up behind the tray so only an edge shows): stamps only mark a document lying directly beneath
   the stamp heads, in the dark strip under the tray. First decide APPROVED or DENIED (section 5; the state
   block shows your own verdict answer), then
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
K. DESK CLUTTER: the state block names an M.O.A. CITATION slip or a flyer (the pink "The Pink Vice" card) lying in
   the working area -- on a stamp landing strip, under the open stamp tray bar, or on the passport -- or an entry
   ticket LEFT BEHIND by an entrant who has already gone (nobody at the window: it can no longer be handed back,
   so it is stowed like a slip, never dragged onto the window). None is the
   entrant's document to check: never stamp it, never hand a citation to the entrant, never hand a flyer back
   instead of the passport. Order: (1) the state block says it lies UNDER THE OPEN STAMP TRAY BAR: close the tray
   first (drag the tray tab, left end of the open stamp bar, onto the "right edge of the desk" target);
   (2) DRAG the slip or flyer (clicking does nothing) onto the "counter shelf left of the desk" target;
   (3) continue with the matching step (C opens the tray again). A flyer may also go back to the entrant together
   with the rest of their papers after the stamped passport was handed back (G2).
E0. A paper that is NOT the passport (the rulebook, the bulletin, a transcript; a citation slip or flyer: step K)
   lies under a stamp (the state
   block says "the RULEBOOK ... lies under the DENIED stamp, not the passport"): stamping it is useless and
   will be refused. Drag the rulebook or bulletin onto the "counter shelf left of the desk" target (other
   papers to the "desk" target), then drag the PASSPORT (from the
   counter shelf or the desk) to the strip under the stamp you need. The rulebook is never needed on Day 1.
E. The stamp tray is open, the passport lies under a stamp head (the state block says "The passport is
   under: APPROVED" or "DENIED"), and it is NOT stamped yet (no stamp ink is read on it, see F): decide with
   section 5, then click the stamp the passport is lying under; if you want the
   other decision, first drag the passport to the other strip -- and if another paper (the entry ticket) lies
   under the stamp you need, first drag that paper to the "desk" target (E0). Never click the stamp of the
   decision you do not want, not even because the passport lies under it. ONE click. If the state block says a paper (the
   entry ticket) lies across the open passport, first drag that paper off it onto the "clear desk space" /
   "desk" target so the passport lies clear, then drag the passport by its own visible part. Clicking the stamp the passport is
   NOT under stamps nothing (the press is refused). Decide only when every reading today's rule needs is known
   -- the state block lists your own readings (issuing country, and from Day 2 the EXP. date and ISS. city, on
   Day 3 the entry ticket of a foreigner). If one is not read yet (your verdict was cannot_decide_yet), do not
   stamp: drag the passport (or the ticket) to the "desk" target so it can be read, then put the passport under
   the stamp you need.
H. INSPECT MODE (the state block says "Inspect mode is ON": desk darkened, red dotted frame, red text
   HIGHLIGHT DISCREPANCIES): documents cannot be moved and stamps cannot be used while it is on. Click the red
   inspect-mode button at the lower right of the desk once to leave it, then continue with the matching step.
   Inspect mode is not needed on Days 1-3 except in step N (no passport presented): the button is only offered
   while inspect mode is on or in step N, and in step N you stay in inspect mode until the interrogation is done.
F. The passport IS STAMPED: the state block says "Stamp pressed: APPROVED at tick N" or "DENIED at tick N" (your
   press on the stamp the passport lay under was executed). The state block also shows the stamp-ink reading of
   the passport page as a fact: if it says no ink is visible and you see no mark either, the press may have missed
   -- you may press that stamp once more. Otherwise STOP clicking stamps and hand the papers back by dragging them onto the person at the window ("the
   entrant at the booth window -- drop documents ON THE PERSON to hand them back"). ORDER: the entrant leaves
   the moment the PASSPORT is back, and any paper still on the desk is left behind. So FIRST drag every OTHER
   paper of the entrant's (the entry ticket from Day 3) onto the person, one per turn; the stamped PASSPORT goes
   back LAST. While the state block names an entry ticket on the desk or the counter shelf, hand that back, not
   the passport. The open stamp tray does not have to be closed first; drag a paper by the part that is visible.
F2. WRONG STAMP: the state block says which stamp was pressed. If it was stamped APPROVED but the
   rule (section 5) says DENIED, click DENIED once more -- a DENIED stamp overrules APPROVED -- then hand it
   back. If it was stamped DENIED but should have been APPROVED, it cannot be fixed (DENIED always wins and
   an APPROVED stamp on top does not count): hand it back as it is. The first two mistakes of each day are
   only warnings.
N. NO DOCUMENTS: the state block says "The person at the window has handed over no documents: yes". There is
   nothing to stamp: this entrant is sent away WITHOUT a stamp; the stamp tray tab is not offered.
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
   each paper of theirs still on the desk or the counter shelf (entry ticket ...) onto the entrant. If nobody
   is at the window any more, the entrant has gone: a paper of theirs still lying there is left behind and is
   stowed (step K), not handed back.
G. After the documents were handed back (the state block says so) the person leaves by themselves; wait
   while they walk away. When the window is empty and nothing is on the counter: go back to A and click the
   loudspeaker to call the next person.
An entrant is finished only after their passport is stamped AND handed back. Clicking the loudspeaker
while someone is still at the window does nothing.

5. DECIDING: APPROVED OR DENIED
- Day 1, 1982.11.23: the ONLY rule is the issuing country (printed in large letters at the bottom of the
  passport, e.g. ARSTOTZKA). Issuing country ARSTOTZKA -> APPROVED. Any other country -> DENIED.
  Expiry date is NOT a Day 1 rule; do not deny anyone on Day 1 for expiry.
- Day 2, 1982.11.24 (expiry and city checks start today): foreigners may enter too. APPROVED if not
  expired (expiry after 1982.11.24) and the ISS. (issuing) city belongs to the
  passport's country; otherwise DENIED. Valid issuing cities (rulebook Regional Map): ARSTOTZKA: Orvech
  Vonor, East Grestin, Paradizna; ANTEGRIA: St. Marmero, Glorian, Outer Grouse; IMPOR: Enkyo, Haihan,
  Tsunkeido; KOLECHIA: Yurko City, Vedor, West Grestin; OBRISTAN: Skal, Lorndaz, Mergerous; REPUBLIA: True
  Glorian, Lesrenadi, Bostan; UNITED FEDERATION: Great Rapid, Shingleton, Korista City.
- Day 3, 1982.11.25: Arstotzkans need a valid passport only. Foreigners also need an entry ticket dated
  1982.11.25; no ticket or a different date -> DENIED.
A reading the rule needs that is not read yet (country, EXP. date, ISS. city, a foreigner's ticket on Day 3) is
  not a reason to approve or to deny: the verdict cannot be decided yet; get the paper read first (desk target).
The first entrant of day 1 is the tutorial; follow the same rule (his passport is Arstotzkan -> APPROVED).

6. BULLETIN, RULEBOOK AND MULTI-PAGE PAPERS
- The bulletin (Ministry of Admission sheet) and the rulebook can lie open on the desk. If one covers the
  passport or the place you need to work, drag it aside to the left part of the desk ("desk" target). They
  are not needed to process day-1 entrants.
- An M.O.A. CITATION slip (printed after a mistake) is the inspector's, not the entrant's: never stamp it, never
  hand it to the entrant. Where it lies out of the way, leave it; in the working area see step K.
- A flyer an entrant puts down with their papers (the pink "The Pink Vice" card) has no rule value and is not a
  document to check: never stamp it. In the working area see step K; otherwise leave it, or hand it back WITH the
  entrant's other papers after the stamped passport.
- Multi-page papers (the bulletin shows "3/4" at its bottom) turn pages when you click their bottom-right
  corner. Do not drag a page corner.

7. OTHER SCREENS
- Main menu (title screen): click STORY.
- Day-select screen ("Select day to continue or start a new game"): a row of day tiles near the top left.
  The goal is the LATEST day available: click the tile with the highest DAY number (on a fresh save the only
  tile is DAY 1 / NEW). It is a tile, not a button. Never click BACK,
  QUIT, or the trash/delete icon on this screen: BACK returns to the main menu and undoes progress, the
  trash icon deletes the save. If no day tile is drawn yet, choose wait.
  Clicking a tile opens a box under the tiles (the day's name, money, family) with CONTINUE and CANCEL text:
  click CONTINUE to load that day (the upper of the two short lines at the bottom of the box; OCR may misspell
  it, e.g. 'COHTIHUE'). Clicking a tile, or the box's own "Day N" title, does nothing while the box is open.
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
  times instead of handing it back. Once the stamp press is on record, hand the passport back.
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
    if no_passport(state, facts):
        lines.append("- The person has presented no documents: there is nothing to stamp; they are sent away "
                     "without a stamp. Ask for the passport with inspect mode (step N)")
    for d in facts.get("docs_named") or []:
        where = "counter shelf" if d["where"] == "counter" else "desk"
        if d["id"] == UNREAD:
            lines.append(f"- Paper on the {where} ({d['pos']}): UNREAD (what it is could not be read: best identity "
                         f"{d.get('raw_id', '?')} at only p={d['p']:.2f}). It is not known to be a ticket, flyer or "
                         "passport" + ("; on the desk it can be read" if where == "counter shelf" else ""))
            continue
        tail = (" -- not needed on Days 1-3; to clear the desk drop it on the 'counter shelf left of the desk' target"
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
    for t, side in facts.get("stamp_clicks") or []:
        lines.append(f"- Stamp pressed: {side.upper()} at tick {t} (your press, the passport lay under the {side.upper()} "
                     "stamp; the passport counts as stamped from then on)")
    for t, side in facts.get("missed_stamps") or []:
        lines.append(f"- The {side.upper()} stamp was clicked at tick {t} while the passport lay under the other "
                     "stamp: nothing was stamped")
    if "strip" in facts and (yes(state, "stamp_tray_open") or facts.get("tray_open_px")):
        pu = facts.get("passport_under") or []
        for side in ("denied", "approved"):
            lines.append(f"- Under the {side.upper()} stamp: " + (
                f"the PASSPORT (p={(facts['strip'][side]['passport_p'] or 0):.2f})" if side in pu
                else under_phrase(facts, side)))
        lines.append("- The passport is under: " + (" and ".join(x.upper() for x in pu) if pu else "none")
                     + ". Both stamps can be pressed; a press on a stamp the passport is not under is refused")
        wrong = [s_ for s_ in ("denied", "approved") if (facts["strip"][s_].get("doc") or "passport") != "passport"
                 and s_ not in pu]
        if wrong:
            lines.append(f"- A paper that is NOT the passport lies under the {' and '.join(w.upper() for w in wrong)} "
                         "stamp (a stamp press there is refused)")
    pop = facts.get("paper_on_passport")
    if pop:
        lines.append(f"- A paper ({pop['id'].upper()}) lies across the open passport on the desk (it covers "
                     f"{int(round(100 * pop['covered']))}% of the passport's box); the 'clear desk space off the "
                     "passport' target takes it off the passport")
    tr = ticket_to_return(state, facts)
    if stamped(state, facts) and tr:
        lines.append(f"- The entrant's ENTRY TICKET still lies on the {tr} (the entrant leaves as soon as the "
                     "passport is returned)")
    v = tod_verdict(state, facts)
    if v:
        lines.append(f"- Your verdict for this entrant (your own answer, {v['where']}): {v['value'].upper()} "
                     f"(p={v['p']:.2f})")
    ra = state.get("passport_returned")
    if facts.get("waiting_docs"):
        said = f"you said 'returned', p={ra['p']:.2f}" if ra else "your answer on an earlier tick"
        lines.append(f"- The entrant's passport has been handed back ({said}) and the entrant is STILL at the window "
                     "with papers of theirs on the desk or counter shelf: they wait for the rest of their documents "
                     "(step G2)")
    elif facts.get("handed_back") is not None:
        lines.append(f"- Documents were handed back at tick {facts['handed_back']}: this entrant is finished and "
                     "leaves by themselves; call the next person once the window is empty")
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
    elif open_ok and day not in ("2", "3"):  # Day 1: expiry is not a rule
        lines.append("- (Day 1: expiry is not checked; only the issuing country decides)")
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
        elif ln.startswith("A reading"):
            cur = "all"   # the cannot-decide-yet line holds on every day
        elif not ln.startswith("  "):
            cur = "tail1"
        if cur in (d, "all") or (cur == "tail1" and d == "1"):
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
    if (facts or {}).get("tray_note"):    # tray toggle loop: the closing tab is excluded (loop.prepare, audit A5)
        h = f"{facts['tray_note']}\n{h}"
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


CLUTTER_NAMES = {"citation": "an M.O.A. CITATION slip", "flyer": "a flyer (The Pink Vice card)"}


def clutter_phrase(c: dict, tray_open: bool) -> str:
    """State-block tail for a citation slip / flyer (loop.clutter_facts: where it lies, whether it is in the way)."""
    if c.get("left_behind"):
        return (" -- LEFT BEHIND: its entrant has already left (nobody is at the window), so it cannot be handed "
                "back any more. It is desk clutter now: drag it onto the 'counter shelf left of the desk' target "
                "(step K), never onto the window")
    who = (" -- the inspector's CITATION slip, NOT the entrant's document: never stamp it, never hand it to the entrant"
           if c["id"] == "citation" else
           " -- the entrant's FLYER, not a document to check: never stamp it, never hand it back instead of the passport")
    if not c["in_way"]:
        return who + "; it lies out of the way (leave it)"
    if c["under_bar"] and tray_open:
        return (who + ". It lies UNDER THE OPEN STAMP TRAY BAR: close the tray first (drag the tray tab onto the "
                "'right edge of the desk' target), then drag it onto the 'counter shelf left of the desk' target (step K)")
    where = (f"ON THE {' and '.join(x.upper() for x in c['strips'])} landing strip"
             + ("s" if len(c["strips"]) > 1 else "") if c["strips"]
             else "on the passport" if c["on_passport"] else "where the stamp strips are")
    return who + f". It lies {where}: drag it onto the 'counter shelf left of the desk' target (step K)"


def clutter_step(state: dict, f: dict):
    """Manual step K: a citation slip / flyer in the working area while the passport is not under a stamp head."""
    cl = [c for c in f.get("clutter") or [] if c["in_way"]]
    if not cl or f.get("passport_under"):
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
    k_step = clutter_step(state, f)
    if k_step:
        return k_step
    # loop13 run 182519 t89-92 (Maslov): the ticket stayed on the counter. Any counter paper that is UNREAD (identity
    # gate) or TOD's entry ticket goes to the desk once the passport is open there (no tick cap: the identity gate
    # keeps a stowed slip from looping it, it is named on the desk)
    if (yes(state, "document_open_on_desk") and any(
            d["where"] == "counter" and d["id"] in (UNREAD, "entry_ticket") for d in f.get("docs_named") or [])):
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
