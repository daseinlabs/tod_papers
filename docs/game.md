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
- **Day-start Bulletin screen:** every day opens with the Ministry bulletin (full-screen text stating rule changes). **Click to dismiss/continue** into the booth. The agent must re-read the ruleset here each day.
- **Day-end "night" / home screen:** after the day, a summary shows **income vs expenses** (rent, food, heat, family). There are selectable line items (e.g. choose to **skip heat** or skip a meal to save money) and a **Continue/Next Day** button. For days 1–5 just pay everything and click **Continue** — no starvation risk. Confirm by clicking the continue/confirm control.
- **Detain prompts:** when a discrepancy is found or a wanted person appears, a **DETAIN** button appears in inspect mode — only relevant from ~Day 5. Not required days 1–3.
- **Scripted story entrants:** dialogue-only interrupts (e.g. **Jorji** on days 2/4) — they still resolve via the normal approve/deny flow; just extra transcript text to click through.
- **Terrorist attacks / combat:** **Days 1–3 have no attack.** The first explosive attack comes much later in the run (commonly cited around **Day 11**, a bomber after the 9th entrant; a booth bomb on Day 15). Sources conflict on an early "Day 2 guard attack" claim — **treat Days 1–5 as attack-free for automation and confirm later days in-game.** Source: Fandom "Terrorism" https://papersplease.fandom.com/wiki/Terrorism

---

## 7. Save / skip to a given day

- **Save location (Windows, Steam/Unity build):**
  `%APPDATA%\3909\PapersPlease\`
  (For this machine: `%APPDATA%\3909\PapersPlease\`.) **Not present yet** — created on first launch. This is the classic 3909 savedata path (the Unity remaster keeps it); saves are **not** in Steam `userdata` / cloud for this title. Source: 3909 Support "Savedata Location" https://3909.zendesk.com/hc/en-us/articles/360057528153-Savedata-Location (reached via search; direct fetch 403s).
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
