# Papers, Please — Automation Scoping (Unity remaster)

Target install: `C:\Program Files (x86)\Steam\steamapps\common\PapersPlease\`
Verified on this machine:
- `PapersPlease.exe`, `GameAssembly.dll` (IL2CPP), `UnityPlayer.dll`.
- **Unity version: `2020.3.34f1`** (read from `PapersPlease_Data/globalgamemanagers`). Note: this is a Unity 2020 LTS build, *not* 2022+.
- Game/content version: **`1.4.11.124`** (`PapersPlease_Data/StreamingAssets/Version.txt`).
- `app.info` contains companyName=`3909`, productName=`PapersPlease`.
- Steam AppID: **239030**.

Do not launch the game (per instructions). All runtime facts below (registry keys, save folder) are the *expected* locations — they do not yet exist on disk because the game has not been run; create/inspect them after first launch.

---

## 1. Launching: windowed at a fixed size

### Native / internal resolution and scaling
Papers, Please renders pixel art at a fixed internal resolution and letterboxes black bars around it. Supported "clean" resolutions are **570×320 (1×), 1140×640 (2×), 1710×960 (3×)** — the engine picks the largest multiple that fits the window and fills the rest with black. (Aspect ≈ 16:9.) Source: PCGamingWiki "Papers, Please", https://www.pcgamingwiki.com/wiki/Papers,_Please

- **Base internal resolution: 570×320.**
- **Recommended automation window: 1710×960 (3× integer scale).** Fits inside a 1920×1080 desktop, gives the largest crisp pixel grid, and makes UI hit-targets large. If the desktop is smaller, fall back to 1140×640 (2×).
- Pick an exact 1×/2×/3× size so the game does **not** letterbox — any other size adds black borders and shrinks the active art region, which shifts every click coordinate.

### Launch flags (Unity standalone player args)
Set these as Steam **Launch Options** (right-click game → Properties → Launch Options) or append to a direct `PapersPlease.exe` invocation:

```
-screen-fullscreen 0 -screen-width 1710 -screen-height 960 -popupwindow
```

- `-screen-fullscreen 0` → windowed (not fullscreen).
- `-screen-width` / `-screen-height` → force the window pixel size (Unity standalone players are DPI-aware, so these are *physical* pixels regardless of the 250% / DPI-240 Windows scaling — the window will be 1710×960 real pixels).
- `-popupwindow` → borderless, no title bar / frame. This makes the window position stable and removes the caption; combine with a window-mover to pin it to (0,0).
- Unity 2020 also accepts `-window-mode exclusive|borderless` on some builds; `-popupwindow` is the reliable borderless flag here.
- Optional: the game reportedly supports a `-savedir <path>` argument to redirect its save folder (useful for snapshotting/restoring day saves). Confirm in-game. Source: 3909 Savedata article (see §7).

Reference for the flags: Unity Manual "Standalone Player command line arguments", https://docs.unity3d.com/6000.2/Documentation/Manual/PlayerCommandLineArguments.html

### In-game settings menu
The remaster's Options/Settings menu exposes **Resolution**, **Windowed/Fullscreen** (borderless-windowed is labelled just "Fullscreen"), and the window is freely resizable by dragging. Prefer the launch flags over the menu so the size is deterministic at startup. Source: PCGamingWiki (above).

### Unity PlayerPrefs registry (persisted window state)
Unity stores screen state under:

```
HKEY_CURRENT_USER\Software\3909\PapersPlease
```

Expected value names (standard Unity ScreenManager keys; created on first run — absent now because the game is unrun):
- `Screenmanager Resolution Width_h<hash>`  (DWORD) — window width
- `Screenmanager Resolution Height_h<hash>` (DWORD) — window height
- `Screenmanager Fullscreen mode_h<hash>`   (DWORD): `1`=Exclusive FS, `2`=FS Window(borderless), `3`=Maximized Window, **`4`=Windowed**
- `Screenmanager Is Fullscreen deprecated_h<hash>` (DWORD, 0/1) on some versions.

The `_h<hash>` suffix is a Unity-generated per-key hash; enumerate the key's values rather than hard-coding the name. To pin windowed mode without launch flags you can pre-seed Width/Height and set Fullscreen mode = 4. Launch flags override these at startup, so flags are the preferred mechanism.
(Company/Product come from PlayerSettings; confirmed `3909`/`PapersPlease` from `app.info`.)

---

## 2. Window title / window-finding

- PlayerSettings **productName serialized in the binary is `PapersPlease`** (no comma/space) — extracted from `globalgamemanagers`. So the default Win32 window **title text is `PapersPlease`**. (Some older/original-engine references call it "Papers, Please"; the Unity build's product string is `PapersPlease`.)
- With `-popupwindow` the title bar is hidden, so **do not rely on the title**. Find the window by:
  - **Window class `UnityWndClass`** (all Unity standalone players use this), and/or
  - **Owning process `PapersPlease.exe`** (most robust).
- Recommended: enumerate top-level windows, match process name `PapersPlease.exe`, take the largest visible client rect.

---

## 3. Screen layout (regions)

The playfield is split into a top "world" half and a bottom "desk" half. Coordinates below are fractions of the **active game art rectangle** (the 570×320-grid area, i.e. the window minus any letterbox bars — with the recommended exact-multiple window there are no bars, so fractions = fractions of the window). These are approximate; calibrate against a first screenshot.

- **Top-left — booth / entrant view (approx x 0.00–0.62, y 0.00–0.52):** the applicant stands behind the glass; their face/photo is compared here. The **shutter lever** (raises/lowers the booth window to call them forward / close) is at the left edge of the booth.
- **Speaker / horn "call next" button:** small speaker icon mounted in the booth, upper-left region (~x 0.03–0.10, y 0.30–0.45). Clicking it summons the next entrant. (On days with a line you also lower the shutter, then raise it.)
- **Counter (bottom-left to center, approx y 0.52–0.80):** the ledge where the entrant **slaps down their documents**. Documents arrive here; you drag them up to the desk, and you **return** documents by dragging them back down onto the counter/entrant.
- **Desk / inspection surface (right half, approx x 0.55–1.00, y 0.52–1.00):** where you drag documents to read them, lay them side by side, and where the stamp tray and rulebook live.
- **Rulebook:** a book icon/object on the desk (left side of desk area). Click to open; it has tabbed pages (Basic Rules, Regions map, Document list, Wanted). Drag documents onto open rulebook pages while in inspect mode to flag mismatches.
- **Stamp tray:** a horizontal **tab/handle at the top-right of the desk**. Pull it **down** to deploy the stamp panel over the passport.
- **Inspect / magnifier button:** a **red button in the lower-right corner of the desk** toggles inspection mode. Source: Steam guide "Gameplay Basics, Documents and Inspect Mode", https://steamcommunity.com/sharedfiles/filedetails/?id=168642147 ; Fandom "Inspection mode", https://papersplease.fandom.com/wiki/Inspection_mode
- **Clock / day progress:** in-game clock runs **6am→6pm**; a clock is shown on the booth wall. The day ends when the clock runs out OR the day's scripted queue empties.

---

## 4. Interaction mechanics (click vs drag)

All object manipulation is **mouse drag** (press-move-release); menu/confirm elements are **clicks**.

- **Call next entrant:** click the **speaker** (and operate the **shutter lever** by dragging it up/down when needed). Click.
- **Receiving documents:** the entrant drops papers on the counter. **Drag** each document from the counter up onto the desk to read it. Papers can be stacked/overlapped; drag to separate and reposition.
- **Returning / handing back documents:** **drag** the document (or the whole stack) back down onto the entrant / booth window area; they take it. To deny entry with no further docs, hand the passport back after stamping.
- **Stamping (APPROVE / DENY):**
  1. Open the entrant's **passport** and drag it onto the desk so the **visa page** is face-up.
  2. **Pull the stamp-tray tab** (top-right of desk) down so the stamp panel covers the passport.
  3. The tray has two stamps: **APPROVED** (entry visa) and **DENIED**. **Click** the chosen stamp — it stamps wherever the passport's visa page sits beneath it. A **"Reason for Denial"** stamp auto-prints the reason on denials.
  4. Push the tray tab back up, then hand the passport back (drag to entrant).
- **Inspect mode:** click the **red inspect button (lower-right)**. The cursor becomes a compare tool. **Click one item, then a second conflicting item** (e.g. passport photo vs the person's face, or two mismatching names/dates across two documents). If they conflict, the game prints **"Discrepancy detected"** and surfaces an **Interrogate** option.
- **Interrogate → detain:** click **Interrogate**; the inspector speaks and a **transcript** dialogue box appears (the back-and-forth text, sometimes with the entrant's reply and a follow-up choice). After a valid discrepancy/interrogation, additional actions unlock: **demand missing document**, **fingerprint/identity check**, **full body scan**, and the **DETAIN** button (red) — click DETAIN to arrest the entrant (earns a bonus from the guard Calensk on later days). Otherwise finish with APPROVE/DENY stamp.
- **Drag event granularity:** Unity reads the OS cursor each frame and the game uses physics-ish momentum on dragged papers. **You must emit intermediate `mouse_move` events along the drag path** (not just mousedown at A then mouseup at B) — a teleport drag is frequently dropped or mis-targeted. Use a down → several interpolated moves (with a few ms between) → up sequence.

---

## 5. Day-by-day rules, story mode (Days 1–5)

Earn **5 credits per entrant processed correctly**. Each day has an **Official Bulletin** shown at day start that states the day's rules; the **Rulebook** on the desk holds the full current ruleset and the regional map. Core verification each day: compare passport **nation** vs who's allowed, **issuing city** vs the region map, **expiration date** vs the current date, **photo** vs the person, and **name/numbers** across all documents.

Sources: Fandom "Official Bulletin" https://papersplease.fandom.com/wiki/Official_Bulletin ; "Rulebook" https://papersplease.fandom.com/wiki/Rulebook ; "Day 2" https://papersplease.fandom.com/wiki/Day_2 , "Day 3" https://papersplease.fandom.com/wiki/Day_3 ; Documents guide https://www.sort-the-court.com/games/papers-please/documents-guide

- **Day 1 — Nov 23, 1982. Required: passport only.**
  Rule: **Only citizens of Arstotzka may enter.** Deny every foreigner. The first entrant is a scripted tutorial (approve). Discrepancy = any non-Arstotzkan nation. (Expiry exists but the dominant check is nationality.) Short, slow day.
- **Day 2 — Nov 24. Required: passport only.**
  New rule: **Foreigners with a valid passport may now enter.** Added provision: **documents must be up to date** — so now actively check the **expiration date** (deny expired), plus photo and the passport fields. (No entry permit yet on day 2.) Jorji Costava may appear with no/invalid passport → deny.
- **Day 3 — Nov 25. Required: passport + ENTRY TICKET (foreigners).**
  New rule: **all foreigners must present a valid entry ticket.** The **ticket date must equal the current day (Nov 25)** — a ticket dated any other day → deny. Citizens still need only a passport. Entry tickets exist only on day 3 (replaced by entry permits afterward).
- **Day 4 — Nov 26. Required (foreigners): passport + ENTRY PERMIT; (citizens): passport + ID card.** *(permit/ID introduction here is per the documents guide; treat the in-game bulletin as authoritative.)*
  Entry-permit checks: name match, passport-number match, purpose, duration, expiration, and the official **seal**. ID-card checks: name, DOB, district, photo, height, weight. Jorji returns with a forged **"Cobrastan"** passport → deny.
- **Day 5 — Nov 27. Required: as Day 4, all prior checks continue.**
  New emphasis: **watch the Wanted / criminal list and DETAIN named suspects** (e.g. Vince Lestrade) — the detain directive is introduced. Keep checking permits/IDs.

### Citations, penalties, money
- Mistakes generate a **Citation** on the *next* entrant. **The first two citations each day are warnings only** (you simply forfeit the 5-credit payment for that entrant, no cash fine). From the 3rd citation onward you are fined, escalating roughly **0, 0, 5, 5, 10, 15, 20, 25, 30…** credits. Source: Fandom "Citation" https://papersplease.fandom.com/wiki/Citation
- **Day length (real time, minimum):** Day 1 ≈ **3 min**, Day 2 ≈ 4 min, Day 3 ≈ 6 min, Days 4–12 ≈ 7 min, Day 13+ ≈ 8 min. In-game 6am→6pm. The day ends when time expires or the queue empties. You can process ~10–15 entrants on a normal day at ~25–30 s each. Source: GameFAQs / speedrun discussion summarized via search.
- **Family money:** income = 5 credits × correctly-processed entrants that day. At day-end you pay fixed expenses (rent, food, heat; later medicine/family upkeep), which rise over time. For Days 1–3 **starvation is not a real risk if you process most of the available entrants correctly** — just maximize correct throughput and avoid citations. Process essentially everyone the game offers each day.

---

## 6. Things that interrupt the processing loop

- **Intro / cutscene:** a one-time opening sequence (labor lottery → arrive at booth) before Day 1. Click/advance through it.
- **Day-start Bulletin screen:** every day opens with the Ministry bulletin (full-screen text stating rule changes). **Click to dismiss/continue** into the booth. The ruleset changes here each day.
- **Day-end "night" / home screen:** after the day, a summary shows **income vs expenses** (rent, food, heat, family). There are selectable line items (e.g. choose to **skip heat** or skip a meal to save money) and a **Continue/Next Day** button. For days 1–5 just pay everything and click **Continue** — no starvation risk. Confirm by clicking the continue/confirm control.
- **Detain prompts:** when a discrepancy is found or a wanted person appears, a **DETAIN** button appears in inspect mode — only relevant from ~Day 5. Not required days 1–3.
- **Scripted story entrants:** dialogue-only interrupts (e.g. **Jorji** on days 2/4) — they still resolve via the normal approve/deny flow; just extra transcript text to click through.
- **Terrorist attacks / combat:** **Days 1–3 have no attack.** The first explosive attack comes much later in the run (commonly cited around **Day 11**, a bomber after the 9th entrant; a booth bomb on Day 15). Sources conflict on an early "Day 2 guard attack" claim — **treat Days 1–5 as attack-free for automation and confirm later days in-game.** Source: Fandom "Terrorism" https://papersplease.fandom.com/wiki/Terrorism

---

## 7. Save / skip to a given day

- **Save location (Windows, Steam/Unity build):**
  `%APPDATA%\3909\PapersPlease\`
  **Not present yet** — created on first launch. This is the classic 3909 savedata path (the Unity remaster keeps it); saves are **not** in Steam `userdata` / cloud for this title. Source: 3909 Support "Savedata Location" https://3909.zendesk.com/hc/en-us/articles/360057528153-Savedata-Location (reached via search; direct fetch 403s).
- **Save files:** `headers.sav`, `names.sav`, `settings.sav`, `stats.sav`, and multiple `save_<date>.sav` (one per reached checkpoint/day). Mac: `~/Library/Application Support/3909/PapersPlease/`; Linux: `~/.local/share/3909/PapersPlease/`.
- **Starting on a given day:** Story mode auto-saves at the start of each day. The main menu's **Continue** resumes the latest day; the game also offers **day select / "jump to day"** for days you have already reached (you cannot skip ahead to unplayed days). For deterministic automation testing, **snapshot the `AppData\Roaming\3909\PapersPlease` folder** after reaching Day N and restore it to replay that day. The `-savedir` launch arg (see §1) lets you point at an isolated save copy per run.

---

## Key source URLs
- PCGamingWiki: https://www.pcgamingwiki.com/wiki/Papers,_Please
- Unity CLI args: https://docs.unity3d.com/6000.2/Documentation/Manual/PlayerCommandLineArguments.html
- Steam mechanics guide: https://steamcommunity.com/sharedfiles/filedetails/?id=168642147
- Documents by day: https://www.sort-the-court.com/games/papers-please/documents-guide
- Fandom: Official Bulletin / Rulebook / Day 2 / Day 3 / Citation / Inspection mode / Terrorism (papersplease.fandom.com)
- Save location: https://3909.zendesk.com/hc/en-us/articles/360057528153-Savedata-Location

---

## Scripted entrants and desk objects, Days 1–3

Sources are listed per subsection. "TAS" below refers to the open-source Papers, Please TAS bot by amari-calipso (https://github.com/amari-calipso/papers-please-tasbot), whose run script hard-codes an input sequence for each scripted entrant. The TAS does not try to avoid citations, so its approve/deny choices are not evidence of correctness; its input sequences are evidence of what the game accepts. Its pixel coordinates are for its own 1156x680 window and are given only to locate objects relative to each other.

### Per-day rules (bulletin wording)
- **Day 1 (1982-11-23):** "Stamp passport ENTRY VISA and return documents to entrant. Entry is restricted to Arstotzkan citizens only." Arstotzkan passport: approve. Any other passport: deny.
- **Day 2 (1982-11-24):** "Foreigners with a valid passport are permitted to enter." Inspection hardware is installed; inspect mode compares two items and reports "Discrepancy Detected". An expired passport (compare with the date at the bottom left) is a deny.
- **Day 3 (1982-11-25):** "Entry for non-citizens is now regulated. All foreigners require a valid ENTRY TICKET." Arstotzkans still need only a passport. The ticket carries a single "valid on" date; it is an effective date, not an expiry date: if it is not 1982.11.25, deny. A foreigner who does not present a ticket: deny (approving an entrant who is missing a required document produces a citation). A second bulletin page explains the no-documents procedure (see Jorji below).
- Sources: https://paperspleaseloc.github.io/ (localization guideline containing the bulletin strings), https://strategywiki.org/wiki/Papers,_Please/Day_1, https://strategywiki.org/wiki/Papers,_Please/Day_2, https://strategywiki.org/wiki/Papers,_Please/Day_3, https://papersplease.fandom.com/wiki/Citation

### Missing-document interrogation (general input sequence)
1. Drag the rulebook from its slot onto the desk; open the **Basic Rules** page.
2. Click the inspect button (red button, lower right of the desk).
3. Click the rule that requires the missing document (e.g. "Entrant must have a passport"; on Day 3 the entry-ticket rule).
4. Click the **empty counter** in front of the entrant (the booth-side area where entrants put their papers; the TAS clicks (275, 525), inside its counter box (8, 455)–(350, 565)).
5. Click the interrogate prompt that appears (the TAS clicks (175, 610), just below the counter).
6. Drag the rulebook back to its slot (the TAS does this; not required by the game).
The entrant then answers: some hand over the missing document, some leave.
- Sources: https://steamsolo.com/guide/gameplay-basics-documents-and-inspect-mode-papers-please/ ("highlight the empty counter and the correlating entry in your R&R-book"), https://papersplease.fandom.com/wiki/Frequent_sticking_points, https://raw.githubusercontent.com/amari-calipso/papers-please-tasbot/main/tas.py (`interrogateMissingDoc`, `noPassport`), https://raw.githubusercontent.com/amari-calipso/papers-please-tasbot/main/modules/constants/screen.py

### Day 1 scripted entrants
| # | Entrant | Correct action |
|---|---|---|
| 1 | Arstotzkan, passport only | Approve, return passport |
| 2 | Imporian | Deny, return passport |
| 3 | Republian | Deny, return passport |
| 4 | Random | By rule |
| 5 | Makes a remark and leaves on his own | Nothing to stamp or return. Call him with the horn, wait for him to leave, call the next entrant |
| 6+ | Random | By rule |
- TAS Day 1 (its comment: "process exactly 12 entrants"): approve, deny, deny, rule check, horn only for #5 (`nextPartial()`, no document input), then 7 rule checks.
- The day does not end until all preset entrants have been handled.
- Sources: https://strategywiki.org/wiki/Papers,_Please/Day_1, https://raw.githubusercontent.com/amari-calipso/papers-please-tasbot/main/runs/AllEndings.py

### Day 2 scripted entrants
| # | Entrant | Correct action |
|---|---|---|
| 1 | Foreigner, valid passport | Approve |
| 2 | Expired passport | Deny |
| 3 | Random | By rule |
| 4 | Arstotzkan, valid | Approve |
| 5 | Not documented as special | By rule |
| 6 | Valid papers plus a Pink Vice flyer | Approve (flyer: see below) |
| 7 | During this entrant an attacker climbs the wall and bombs the checkpoint; the day ends early | Process normally; the attack is a cutscene with no player input |
- None of the sources documents a Day 2 entrant who hands over a message or note; the first note-giving scripted entrants (EZIC) appear later.
- Sources: https://strategywiki.org/wiki/Papers,_Please/Day_2, https://papersplease.fandom.com/wiki/Terrorism, https://speeddemosarchive.com/PapersPlease.html ("Day 2 is first of those" terrorist-attack days)

### Day 3 scripted entrants
| # | Entrant | Correct action |
|---|---|---|
| 1 | Foreigner, valid passport + ticket | Approve |
| 2 | Foreigner, presents passport only (forgot the ticket) | Deny; alternatively interrogate (entry-ticket rule + empty counter) and he hands over the ticket, then judge the full set |
| 3 | Imporian woman, valid papers + Pink Vice flyer | Approve |
| 4 | Random | By rule |
| 5 | Valid papers | Approve |
| 6 | Ticket with the wrong date | Deny |
| 7 | Random | By rule |
| 8 | Jorji Costava, no papers | Interrogate (below); he leaves |
| 9+ | Random | By rule |
- Speedrun notes: on Day 3 "entrants 1, 3, 5 always have correct papers and entrants 2, 6, 8 always have incorrect papers", so denying #2 without interrogation is correct.
- TAS Day 3 sequence: approve (multi-doc), deny passport-only (#2, no interrogation), approve multi-doc (#3), rule check, approve, deny (#6), rule check, then for #8 horn + `noPassport()`, then rule checks until the day ends.
- Sources: https://strategywiki.org/wiki/Papers,_Please/Day_3, https://speeddemosarchive.com/PapersPlease.html, https://raw.githubusercontent.com/amari-calipso/papers-please-tasbot/main/runs/AllEndings.py

### Jorji Costava, Day 3 (first appearance)
- He first appears on **Day 3** as entrant 8 (not Day 2). He puts nothing on the counter and claims Arstotzka is so great that no passport is required. He is not named on this visit.
- There is nothing to stamp and nothing to hand back.
- If the player does nothing, the game shows a hint: "THIS ENTRANT HAS NO DOCUMENTS. To proceed, use INSPECT mode to interrogate." (string `HintMissingInner`). The day does not move on until the interrogation is done.
- Exact input sequence. This is the TAS `interrogateMissingDoc("entrants-must-have-passport")`, coordinates are in its 1156x680 game window:
  1. Drag the rulebook from its desk slot (250,620) onto the desk (750,450), then click the Basic Rules tab.
  2. Click the red INSPECT button at the lower right of the desk (1110,650).
  3. Click the rule line "Entrant must have a passport" on the Basic Rules page. This is highlight 1.
  4. Click the **empty booth counter**, the shelf under the booth window where entrants put their papers (275,525). This is highlight 2. Do not click the entrant. The game shows "DISCREPANCY DETECTED" (`booth_discrepancy`). Clicking the counter does nothing by itself: it is only the second highlight.
  5. Wait about 2 s (TAS `INSPECT_INTERROGATE_TIME` = 2 s after the counter click). Then press **INTERROGATE** (`booth_interrogate`), which appears on the microphone/speaker on the booth-side counter, lower left (175,610). The TAS does this with a plain click: mouseDown and mouseUp at the same point with no movement. The point is the same one the TAS uses as `TRANSCRIPTION_POS` (the transcript sits under that microphone). Guides say the same thing: after a discrepancy the microphone is highlighted, and you "hit that".
  6. The inspector asks, and Jorji answers. He then **leaves on his own**. On Day 3 there is nothing to stamp, no visa slip, and nothing to hand back. The TAS's `noPassport` then just clicks the rulebook bookmark and drags the rulebook back to its slot (250,620). Next comes the horn (345,175).
- Failsafe in the TAS: about 1.5 s after the counter click it checks the desk area for "MATCHING DATA" or "NO CORRELATION". If one appears, the pair did not register: it clicks INSPECT to leave and does not interrogate.
- Microphone role: it is the INTERROGATE trigger, and from about Day 6 it also holds the audio transcript. The transcript is a paper you **drag** out from under the mic (175,610) onto the desk. The TAS drags it to (550,450). It is not needed on Day 3. "Entrant must have passport" / visa-slip DENIED belongs to later days: when the passport is missing, interrogating gives a temporary slip that you stamp DENIED. That does not happen on Day 3.
- Uncertain: no source says the mic must be *dragged up* to interrogate. The TAS, which plays the real game, clicks it. A drag that starts and ends on the mic probably also works, because it is a press and release on the button. If clicking the mic does nothing live, check three things: (a) the counter highlight registered (DISCREPANCY DETECTED is visible), (b) the ~2 s delay was waited, (c) the click landed on the mic graphic and not on the booth counter.
- Later visits: Day 4 crude fake passport, Day 6 genuine passport without entry permit, Day 11 valid papers.
- Sources: https://raw.githubusercontent.com/amari-calipso/papers-please-tasbot/main/tas.py (`interrogateMissingDoc`, `interrogate`, `interrogateFailsafe`, `noPassport`), https://raw.githubusercontent.com/amari-calipso/papers-please-tasbot/main/modules/constants/screen.py (`INTERROGATE_BUTTON = TRANSCRIPTION_POS = (175, 610)`, `NO_PASSPORT_CLICK_POS = (275, 525)`, `INSPECT_BUTTON = (1110, 650)`, `WINDOW_SIZE = (1156, 680)`), https://raw.githubusercontent.com/amari-calipso/papers-please-tasbot/main/modules/constants/delays.py, https://paperspleaseloc.github.io/ (`booth_interrogate`, `booth_discrepancy`, `HintMissingInner`), https://www.theaveragegamer.com/2013/08/20/papers-please-tips-and-common-problems/ ("highlight the empty counter and the relevant rule"; transcript under "that speaker-looking thing"), https://papersplease.fandom.com/wiki/Inspection_mode, https://papersplease.fandom.com/wiki/Audio_transcript, https://papersplease.fandom.com/wiki/Jorji_Costava, https://strategywiki.org/wiki/Papers,_Please/Day_3

### Pink Vice flyer (Day 2 entrant 6, Day 3 entrant 3)
- The entrant places her normal papers plus a small flyer ("Come to Pink Vice. Ask for Ava.") on the counter. Entrants carrying the flyer always have valid papers: approve.
- The flyer has no rule value. It cannot be thrown away; guides say to drag it out of the way (a desk corner, or the far left of the booth counter) and keep it.
- The TAS treats it as an extra paper: drags it from the counter to a desk slot, and after stamping drags every non-passport paper, the flyer included, to the entrant. Both keeping it and handing it back are therefore accepted.
- Sources: https://strategywiki.org/wiki/Papers,_Please/Day_2, https://strategywiki.org/wiki/Papers,_Please/Day_3, https://papersplease.fandom.com/wiki/The_Pink_Vice, https://raw.githubusercontent.com/amari-calipso/papers-please-tasbot/main/modules/documentStack.py

### Citation slips
- A citation is printed into the booth shortly after the entrant leaves when protocol was violated (approving with a discrepancy or a missing document, denying an entrant whose papers are all correct). The first two per day are warnings ("WARNING ISSUED - NO PENALTY", "LAST WARNING - NO PENALTY"); later ones assess a credit penalty.
- Citations are for the inspector; they are not handed to the entrant. They do not block calling or processing the next entrant: the TAS contains no citation handling at all and its document loop works through days on which it takes citations.
- None of the sources found states whether citation slips must be moved, or whether they stack in one spot or spread over the desk.
- Sources: https://en.wikipedia.org/wiki/Papers,_Please, https://papersplease.fandom.com/wiki/Citation, https://paperspleaseloc.github.io/, https://digitalst0rytelling.wordpress.com/2016/02/19/vigilance-in-papers-please/, https://raw.githubusercontent.com/amari-calipso/papers-please-tasbot/main/tas.py

### Scripted vs random entrants, determinism
- Each day mixes scripted entrants at fixed positions with randomly generated ones. Fixed positions above: Day 1 #1–3 and #5; Day 2 #1, 2, 4, 6, 7; Day 3 #1–3, 5, 6, 8. "Every day, after entrant 10 everyone is always completely random and unpredictable."
- Scripted entrants keep their position, dialogue and document validity on every play. Random entrants differ between playthroughs; the developer's test harness seeds each playthrough with a different random seed, so random entrants are not guaranteed to repeat after a restart.
- Day 1 and Day 3 run until 18:00 game time (and not before all preset entrants are handled), so the total count depends on speed; Day 2 ends early with the bombing during entrant 7.
- Sources: https://speeddemosarchive.com/PapersPlease.html, https://en.wikipedia.org/wiki/Papers,_Please, https://dukope.com/devlogs/papers-please/mobile/, https://strategywiki.org/wiki/Papers,_Please/Day_1, https://steamsolo.com/guide/gameplay-basics-documents-and-inspect-mode-papers-please/

### Inputs other than stamping and dragging papers
- **Horn/speaker** click: calls the next entrant (the first click starts the 06:00–18:00 clock).
- **Inspect mode** button (lower right of the desk): required on Day 3 for Jorji; optional for Day 3 entrant 2; available from Day 2 for discrepancies.
- **Rulebook**: drag out and click the Basic Rules page; required on Day 3 for Jorji.
- **Interrogate prompt**: click after linking two items in inspect mode.
- **Transcript**: usable in inspect mode to compare spoken answers with documents; not required on Days 1–3.
- End of day: expense checkboxes (food, heat, medicine) on the summary screen, then the sleep button.
- Sources: https://steamsolo.com/guide/gameplay-basics-documents-and-inspect-mode-papers-please/, https://raw.githubusercontent.com/amari-calipso/papers-please-tasbot/main/tas.py, https://raw.githubusercontent.com/amari-calipso/papers-please-tasbot/main/runs/AllEndings.py
