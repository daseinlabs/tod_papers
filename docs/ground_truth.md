# Ground-truth game state (process-memory reader)

`src/tod_papers/gt.py` reads Papers, Please's own state out of the running
process so that runs can be labelled and scored against the game's truth.
**These values are for reward, labels and evaluation only. They must never be
put into a TOD request or any other model input.**

- Read-only: `OpenProcess` + `ReadProcessMemory` (via `pymem`) and
  `VirtualQueryEx`. Nothing is written to the process, nothing is injected,
  and no input is sent to the game window.
- Pinned to game **1.4.11.124-S** (Player.log `[Game] Version: 1.4.11.124`),
  `GameAssembly.dll` PE timestamp `0x641932F2`, SizeOfImage `0xFE3000`
  (MD5 `1746be42a62af26b3303609dc806ff04`), `global-metadata.dat` v27
  (MD5 `74102d0cb1522df613e9a86d36e44c4f`). `Reader()` checks the PE header
  and refuses to run on any other build.

## Usage

```
py -3.13 -m venv .venv-gt
.venv-gt\Scripts\pip install -r requirements-gt.txt

.venv-gt\Scripts\python tools\gt_probe.py                 # one JSON snapshot
.venv-gt\Scripts\python tools\gt_probe.py --watch         # a line per change, 10 Hz
.venv-gt\Scripts\python tools\gt_probe.py --watch --hz 20 --jsonl runs\gt.jsonl
```

From Python:

```python
from tod_papers import gt
s = gt.snapshot()   # never raises; {"ok": False, "error": ...} if unreadable
```

The first call attaches and locates the `Game` object, which takes about 1 s
(a heap scan, see below). Later calls take about 2-7 ms each, so polling at
10 Hz or faster is fine. If the process restarts, the next call re-attaches.

## Fields in a snapshot

| key | type | source (class.field @ offset) |
|---|---|---|
| `screen` | str | class name of `Game.gameScreen` @0x30 (`DayScreen`, `NightScreen`, menus...) |
| `day` | int | `play.day.Day.id` @0x10, through `Game.day` @0x28 |
| `day_date_jd`, `date` | float, ISO str | `Day.date` @0x18, a Julian day number; `date` is its calendar date (day 1 = 1982-11-23, day 2 = 1982-11-24, matching the in-game date) |
| `day_min_travelers` | int | `Day.minTravelers` @0x38: the shift does not end before this many entrants, even once the clock reaches 18:00 |
| `day_duration_min` | float | `Day.durationInMinutes` @0x40 (real-time shift length) |
| `day_made` | int | `Day.numMadeTravelers` @0x5C (entrants generated so far, including the current one) |
| `day_processed_paid` / `_unpaid` | int | `Day.numProcessedTravelersPaid` @0x60 / `...Unpaid` @0x64 (entrants processed after 18:00 are counted as unpaid) |
| `day_processed` | int | sum of the two above (the entrants processed today) |
| `day_detains` | int | `Day.numDetains` @0x88 |
| `day_bribe_money` | int | `Day.bribeMoney` @0xFC |
| `citations` | list | `Day.citations` @0x138 (haxe `Array<data.Citation>`). Each entry is `{type, penalty_cost}`. `type` comes from the class name of `Citation.type` @0x10 (`CitationType_WARNING` / `_LASTWARNING` / `_PENALTY`), with `Enum._hx_index` @0x10 as a fallback. `penalty_cost` is `Citation.penaltyCost` @0x28 |
| `num_citations`, `num_penalties`, `penalty_cost` | int | derived from `citations`, matching what `Day.get_numCitations` / `get_numPenalties` / `get_penaltyCost` compute |
| `stat_processed`, `stat_approved`, `stat_denied`, `stat_detained`, `stat_citations`, `stat_stamps`, `stat_day` | int or None | story facts `Game/Stat/*`, read through `Day.run` @0x140 -> `DayRun.storyState` @0x18 -> `StoryState.facts` @0x10 -> `FactSet.facts` @0x10 (a `StringMap` of `data.Fact`, whose `.value` @0x18 is a `FactValue` with `.text` @0x10). These are campaign-cumulative. A key is absent (None) until the game first writes it |
| `game_num_citations` | int or None | story fact `Game/NumCitations` |
| `savings`, `rent` | int or None | story facts `Night/Savings`, `Night/Rent` (the family budget that the night screen shows). None before the first night |
| `booth_time` | float | `Booth.boothClock` @0xF8 -> `app.Clock.time` @0x10 |
| `clock_hour`, `clock` | float, "HH:MM" | in-game wall clock on the booth console: `Booth.consoleEnt` @0xA8 -> `ConsoleEnt.consoleClock` @0xA0 -> `ConsoleClock.hour` @0x88 |
| `console_traveler_count` | int | `ConsoleEnt.travelerCount` @0x80 |
| `eng_*` | | `Booth.engine` @0x98 -> `BoothEngine`: `mistakeCaught` @0x20, `numTravelers` @0x30, `haveStampedSomething` @0x37, `justMadeMistake` @0x38 |
| `entrant` | dict or None | current entrant, see below |

The booth fields (`clock`, `eng_*`, `entrant`) are present only while
`screen == "DayScreen"`. The booth is reached through `DayScreen.booth` @0xB0.

### Current entrant and verdicts

`BoothEngine.env` @0x10 -> `BoothEnv`, `BoothEnv.traveler` @0x18 -> `play.day.Traveler`.

- `name` (`Traveler.name` @0x28 -> `TravelerName.first` @0x10 / `.last` @0x18),
  `id_number` @0x30, `nationality` @0x38, `spec_id` @0xB8, `force_deny` @0x68,
  `detained` @0xEB, `gave_stamped_passport` @0xF9.
- `invalid_fact_paths`: `BoothEnv.invalidFactPaths` @0x38, the fact paths the
  game deliberately made inconsistent for this entrant, for example
  `Traveler/Nationality`.
- `noticeable_errors`: the subset of those paths whose
  `FactDef.noticeErrors` @0x50 is true. FactDefs are found through
  `BoothEnv.run` @0x10 -> `BoothEnvRun.db` @0x10 -> `Db.factLib` @0x58 ->
  `FactLib.factDefs` @0x20, and the result is cached. This mirrors
  `BoothEnv.hasErrors(false)`, which loops over `invalidFactPaths` and calls
  `FactLib.getShouldNoticeError(path)` (that call returns `factDefs[path].noticeErrors`).
- `correct_verdict`: `"DENIED"` if `noticeable_errors` is non-empty, otherwise
  `"APPROVED"`. This comes from the disassembly of
  `BoothEngine.handleEvent` (RVA 0x468A30). When the entrant leaves, the
  engine compares `StampApproval.isApproved(givenApproval)` with
  `env.hasErrors(false)`. If the two are equal (an approval with errors, or a
  denial without errors), it collects `env.getCiteText()` lines into the
  citation reasons. Those reasons then feed `Day.addCitation`, and the
  `NumCitations` fact is incremented.
- `given_verdict`: the `Traveler/Approval` fact in `BoothEnv.facts` @0x60.
  This is the same fact that `BoothEngine.get_givenApproval` reads and passes
  to `StampApproval.fromString`. It is `APPROVED`/`DENIED` once the passport
  has been stamped, and None before that.
- `verdict_correct`: `given_verdict == correct_verdict`, or None while no stamp
  has been given.

## How the root is found (offset provenance)

1. **Dump.** Il2CppDumper v6.7.46 (github.com/Perfare/Il2CppDumper, release
   `Il2CppDumper-win-v6.7.46.zip`) was run on `GameAssembly.dll` and
   `PapersPlease_Data\il2cpp_data\Metadata\global-metadata.dat`. It needed no
   special mode: metadata v27, no encryption. All field offsets above come
   from its `dump.cs`, all method RVAs from `script.json`, and the
   `Il2CppClass` layout from `il2cpp.h`. The dump is kept out of the repo
   (`.tools/`, gitignored).
2. **Klass pointer.** `script.json` lists the metadata-usage slot
   `Game_TypeInfo` at RVA `0xC5E6F0`. Once the game is running, the qword at
   `GameAssembly.dll + 0xC5E6F0` holds the `Il2CppClass*` for `Game`.
   `klass->name` @0x10 is checked to be `"Game"`. A value with the low bit set
   means the slot is still an unresolved metadata token.
3. **Instance.** The usual route for IL2CPP statics (`klass->static_fields`
   @0xB8 -> field) does not work here. The only static that would hold the
   game object is `Main.game`, and `Main`'s class is initialised with
   `static_fields == NULL`, because the game is booted from the
   `HostUnity` MonoBehaviour and keeps its reference in an instance field
   (`HostUnity.game` @0x68). So the reader scans committed private
   `PAGE_READWRITE` regions for 8-byte-aligned qwords equal to the `Game`
   klass pointer. It accepts a candidate only if `[+0x10]` is an object of
   class `Platform` and `[+0x18]` is a `Bootstrap`. This match was unique in
   testing. The address is cached and revalidated on every snapshot, and the
   scan reruns if validation fails.
4. Everything else is a plain pointer walk from `Game` with the `dump.cs`
   offsets. Haxe containers are walked by hand: `Array` = `{int length @0x10,
   object[] __a @0x18}`, `StringMap` = `{string[] _keys @0x18, object[] vals
   @0x20, int nBuckets @0x28}`, and managed `object[]` data starts at +0x20.
5. Semantics that are not visible from the field names were confirmed by
   disassembling the game's own accessors with capstone:
   `StampApproval.isApproved` is `flags & 2`, and `isDenied` is `flags & 4`
   with `REASONED = 8`. `get_givenApproval` reads `Traveler/Approval` from
   `env.facts`. `getShouldNoticeError` returns `FactDef.noticeErrors`.
   `handleEvent` contains the citation decision described above.

## Validation done (live, day 1 of a fresh campaign)

- `day` = 1 and `clock` = 18:00. The 3-minute day-1 shift had run out, but
  `day_min_travelers` = 5 kept the day open. After 18:00, entrants counted
  as `day_processed_unpaid`. The day ended (`screen` went
  `DayScreen` -> `NightScreen`) exactly when `day_processed` reached 5.
- On the night screen, `savings` went from None to 10 (the `Night/Savings`
  fact being written). Then `screen` went `NewsScreen` -> `DayScreen` with `day` = 2,
  `date` = 1982-11-24 (the run's OCR read "November 24th, 1982"),
  `clock` = 06:00, and the per-day counters reset to 0. The savings value was
  not cross-checked against the night summary on screen.
- During day 2, `clock` advanced continuously (12:49 -> 13:02 over about 4.5 s of
  wall time), and it stopped advancing while the game was paused.
- `day_processed` and the lifetime `stat_processed` both went up by one each
  time an entrant left the booth. `day_made` went up when the next entrant
  was called.
- For day 1 (the rule is Arstotzkan citizens only), non-Arstotzkan entrants
  showed `noticeable_errors == ['Traveler/Nationality']` and `correct=DENIED`.
  An Arstotzkan showed `correct=APPROVED`. `given_verdict` changed from None
  to `DENIED` at the moment the stamp was applied.
- Citations: the reader and the type decoding follow the dump and
  disassembly. See the limits below for whether one was observed live.

## Limits

- **Build-pinned.** Any game update invalidates RVAs and offsets. Rerun
  Il2CppDumper and update the constants at the top of `gt.py`.
  The PE-header check fails closed.
- Pointer chains change with the screen. Booth fields exist only on
  `DayScreen`, and `Game.day` may still point at the previous day during the
  night and menus.
- Reads are not atomic with respect to the game thread. A snapshot taken
  during a GC or a scene change can fail with `ok: False`. Just poll again.
- `correct_verdict` covers the approve/deny decision only. The game can also
  issue citations for other protocol breaches handled in the same
  `handleEvent` path, such as a missing denial reason when that rule is
  active (`missing_reason`) or an unauthorised confiscation
  (`unauth_confiscation`). Those appear only as a new entry in `citations`. Detain decisions and special
  scripted entrants (`force_deny`, `spec_id`) are reported but not folded
  into `correct_verdict`.
- The check in `handleEvent` is gated by `BoothEngine.mistakeCaught` (@0x20,
  exported as `eng_mistake_check`) and is skipped for detained entrants.
- A citation is appended to `Day.citations` when the entrant leaves. The
  printed slip appears later, with the next entrant.
- `savings` and `rent` come from story facts that the night screen writes, so
  they are None on day 1 before the first night. Credits earned during a day
  are not stored as a separate field. The game derives them at night from
  `numProcessedTravelersPaid`, penalties and bribes.
