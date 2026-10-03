"""Ground-truth game-state reader for Papers, Please (read-only process memory).

For reward / labelling / eval ONLY.  Nothing produced here may be put into a
TOD request.

Attaches to a running PapersPlease.exe (Unity 2020.3 IL2CPP, x64) with
ReadProcessMemory via pymem, finds the single ``Game`` object and walks
pointer chains whose field offsets come from an Il2CppDumper dump of game
build 1.4.11.124 (GameAssembly.dll PE timestamp 0x641932F2).  See
docs/ground_truth.md for provenance of every offset.

    from tod_papers import gt
    snap = gt.snapshot()          # dict, cheap (~1-3 ms after first attach)

No writes, no code injection, no input to the game window.
"""
from __future__ import annotations

import ctypes
import datetime
import struct
import time
from ctypes import wintypes

# --------------------------------------------------------------------------
# Build pin.  Offsets below are only valid for this exact GameAssembly.dll.
# --------------------------------------------------------------------------
PROCESS = "PapersPlease.exe"
MODULE = "GameAssembly.dll"
PINNED_PE_TIMESTAMP = 0x641932F2
PINNED_SIZE_OF_IMAGE = 0xFE3000
GAME_VERSION = "1.4.11.124"

# RVA of the metadata-usage slot "Game_TypeInfo" (Il2CppDumper script.json).
RVA_GAME_TYPEINFO = 0xC5E6F0

# Il2CppClass (v27, x64)
K_NAME = 0x10
K_NAMESPACE = 0x18
K_PARENT = 0x58

# System.String
STR_LEN = 0x10
STR_CHARS = 0x14
# System.Array (object[])
SZARR_LEN = 0x18
SZARR_DATA = 0x20
# haxe.root.Array
HXARR_LEN = 0x10
HXARR_A = 0x18
# haxe.ds.StringMap
SMAP_KEYS = 0x18
SMAP_VALS = 0x20
SMAP_NBUCKETS = 0x28
# haxe.lang.Enum
ENUM_INDEX = 0x10

# Game
GAME_DAY = 0x28
GAME_SCREEN = 0x30
# play.day.Day
DAY_ID = 0x10
DAY_DATE = 0x18
DAY_MIN_TRAVELERS = 0x38
DAY_DURATION_MIN = 0x40
DAY_NUM_MADE = 0x5C
DAY_PAID = 0x60
DAY_UNPAID = 0x64
DAY_NUM_DETAINS = 0x88
DAY_WAITING_LINE = 0xB0
DAY_BRIBE = 0xFC
DAY_CITATIONS = 0x138
DAY_RUN = 0x140
# play.day.DayRun
DAYRUN_DB = 0x10
DAYRUN_STORY = 0x18
# play.StoryState
STORY_FACTS = 0x10
# data.FactSet
FACTSET_MAP = 0x10
# data.Fact / data.FactValue
FACT_VALUE = 0x18
FACTVALUE_TEXT = 0x10
# data.Citation
CIT_TYPE = 0x10
CIT_PENALTY_COST = 0x28
# play.screen.DayScreen
DAYSCREEN_BOOTH = 0xB0
# play.day.booth.Booth
BOOTH_ENGINE = 0x98
BOOTH_CONSOLE = 0xA8
BOOTH_CLOCK = 0xF8
# play.day.booth.ConsoleEnt / ConsoleClock
CONSOLE_TRAVELER_COUNT = 0x80
CONSOLE_CLOCK = 0xA0
CONSOLECLOCK_HOUR = 0x88
# app.Clock
CLOCK_TIME = 0x10
# play.day.BoothEngine
ENG_ENV = 0x10
ENG_MISTAKE_CAUGHT = 0x20
ENG_NUM_TRAVELERS = 0x30
ENG_HAVE_STAMPED = 0x37
ENG_JUST_MADE_MISTAKE = 0x38
# play.day.BoothEnv / BoothEnvRun
ENV_RUN = 0x10
ENVRUN_DB = 0x10
ENV_TRAVELER = 0x18
ENV_INVALID_FACT_PATHS = 0x38
ENV_FACTS = 0x60
# play.day.Traveler
TRV_NAME = 0x28
TRV_ID_NUMBER = 0x30
TRV_NATIONALITY = 0x38
TRV_SPEC_ID = 0xB8
TRV_FORCE_DENY = 0x68
TRV_DETAINED = 0xEB
TRV_GAVE_STAMPED_PASSPORT = 0xF9
# data.TravelerName
TNAME_FIRST = 0x10
TNAME_LAST = 0x18
# Db / FactLib / FactDef
DB_FACTLIB = 0x58
FACTLIB_FACTDEFS = 0x20
FACTDEF_NOTICE_ERRORS = 0x50

CITATION_TYPES = ("WARNING", "LASTWARNING", "PENALTY")  # CitationType ctor order

STAT_KEYS = {
    "Game/Stat/NumProcessed": "stat_processed",
    "Game/Stat/NumApproved": "stat_approved",
    "Game/Stat/NumDenied": "stat_denied",
    "Game/Stat/NumDetained": "stat_detained",
    "Game/Stat/NumCitations": "stat_citations",
    "Game/Stat/NumStamps": "stat_stamps",
    "Game/Stat/Day": "stat_day",
    "Game/NumCitations": "game_num_citations",
    "Night/Savings": "savings",
    "Night/Rent": "rent",
}


class GTError(RuntimeError):
    pass


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


_VirtualQueryEx = ctypes.windll.kernel32.VirtualQueryEx
_VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_ulonglong, ctypes.POINTER(_MBI), ctypes.c_size_t]
_VirtualQueryEx.restype = ctypes.c_size_t


class Reader:
    """Read-only view of one PapersPlease.exe process."""

    def __init__(self, check_build: bool = True):
        import pymem
        import pymem.process

        try:
            self.pm = pymem.Pymem(PROCESS)
        except Exception as e:  # process not found / access denied
            raise GTError(f"cannot open {PROCESS}: {e}") from e
        mod = pymem.process.module_from_name(self.pm.process_handle, MODULE)
        if mod is None:
            raise GTError(f"{MODULE} not loaded")
        self.base = mod.lpBaseOfDll
        if check_build:
            self._check_build()
        self._strcache: dict[int, str] = {}
        self._klasscache: dict[int, str] = {}
        self._notice: dict[str, bool] = {}
        self.game = 0
        self.game_klass = self.u64(self.base + RVA_GAME_TYPEINFO)
        if not self.game_klass or self.game_klass & 1 or self.klass_name(self.game_klass) != "Game":
            raise GTError("Game_TypeInfo slot not initialised / wrong build")

    # ---------------- raw reads ----------------
    def _rb(self, addr: int, n: int) -> bytes:
        return self.pm.read_bytes(addr, n)

    def u64(self, a: int) -> int:
        return struct.unpack("<Q", self._rb(a, 8))[0]

    def i32(self, a: int) -> int:
        return struct.unpack("<i", self._rb(a, 4))[0]

    def f64(self, a: int) -> float:
        return struct.unpack("<d", self._rb(a, 8))[0]

    def u8(self, a: int) -> int:
        return self._rb(a, 1)[0]

    def _check_build(self):
        hdr = self._rb(self.base, 0x400)
        pe = struct.unpack_from("<I", hdr, 0x3C)[0]
        ts = struct.unpack_from("<I", hdr, pe + 8)[0]
        soi = struct.unpack_from("<I", hdr, pe + 24 + 56)[0]
        if ts != PINNED_PE_TIMESTAMP or soi != PINNED_SIZE_OF_IMAGE:
            raise GTError(
                f"GameAssembly.dll build mismatch (timestamp {ts:#x}, image {soi:#x}); "
                f"offsets are pinned to {PINNED_PE_TIMESTAMP:#x}/{PINNED_SIZE_OF_IMAGE:#x}. Re-dump."
            )

    def string(self, p: int, maxlen: int = 512) -> str | None:
        if not p:
            return None
        s = self._strcache.get(p)
        if s is not None:
            return s
        n = self.i32(p + STR_LEN)
        if n < 0 or n > 100000:
            return None
        s = self._rb(p + STR_CHARS, min(n, maxlen) * 2).decode("utf-16le", "replace")
        if len(self._strcache) > 20000:
            self._strcache.clear()
        self._strcache[p] = s  # managed strings are immutable; address reuse after GC is rare but
        return s               # possible, so callers only cache keys of long-lived maps.

    def string_nocache(self, p: int, maxlen: int = 512) -> str | None:
        if not p:
            return None
        n = self.i32(p + STR_LEN)
        if n < 0 or n > 100000:
            return None
        return self._rb(p + STR_CHARS, min(n, maxlen) * 2).decode("utf-16le", "replace")

    def klass_name(self, k: int) -> str:
        n = self._klasscache.get(k)
        if n is None:
            n = self.pm.read_string(self.u64(k + K_NAME), 128)
            self._klasscache[k] = n
        return n

    def obj_class(self, o: int) -> str | None:
        return self.klass_name(self.u64(o)) if o else None

    def ptrs(self, a: int, n: int) -> tuple[int, ...]:
        if n <= 0:
            return ()
        return struct.unpack(f"<{n}Q", self._rb(a, 8 * n))

    def hx_array(self, arr: int) -> list[int]:
        """Elements (object pointers) of a haxe Array<Dynamic>."""
        if not arr:
            return []
        n = self.i32(arr + HXARR_LEN)
        if n <= 0:
            return []
        a = self.u64(arr + HXARR_A)
        return list(self.ptrs(a + SZARR_DATA, min(n, 4096)))

    def string_map(self, m: int, keys_stable: bool = False) -> dict[str, int]:
        """haxe.ds.StringMap -> {key: value pointer}."""
        if not m:
            return {}
        nb = self.i32(m + SMAP_NBUCKETS)
        if nb <= 0 or nb > 1 << 20:
            return {}
        ks = self.ptrs(self.u64(m + SMAP_KEYS) + SZARR_DATA, nb)
        vs = self.ptrs(self.u64(m + SMAP_VALS) + SZARR_DATA, nb)
        rd = self.string if keys_stable else self.string_nocache
        return {rd(k): v for k, v in zip(ks, vs) if k}

    def factset(self, fs: int) -> dict[str, str | None]:
        out = {}
        if not fs:
            return out
        for k, fact in self.string_map(self.u64(fs + FACTSET_MAP)).items():
            fv = self.u64(fact + FACT_VALUE) if fact else 0
            out[k] = self.string_nocache(self.u64(fv + FACTVALUE_TEXT)) if fv else None
        return out

    # ---------------- root discovery ----------------
    def _is_game(self, g: int) -> bool:
        try:
            return (
                self.u64(g) == self.game_klass
                and self.obj_class(self.u64(g + 0x10)) == "Platform"
                and self.obj_class(self.u64(g + 0x18)) == "Bootstrap"
            )
        except Exception:
            return False

    def find_game(self) -> int:
        """Scan committed private RW memory for the (single) Game instance.

        Main.game (the only static that would hold it) is never initialised
        in this build -- the game is booted from the HostUnity MonoBehaviour --
        so we locate the object by its klass pointer and validate its first
        two fields' types.  ~0.5-2 s once; the result is cached.
        """
        if self.game and self._is_game(self.game):
            return self.game
        needle = struct.pack("<Q", self.game_klass)
        mbi = _MBI()
        a = 0
        h = self.pm.process_handle
        while a < 0x7FFFFFFFFFFF and _VirtualQueryEx(h, a, ctypes.byref(mbi), ctypes.sizeof(mbi)):
            base, size = mbi.BaseAddress, mbi.RegionSize
            if mbi.State == 0x1000 and mbi.Type == 0x20000 and mbi.Protect == 0x04 and size < 1 << 30:
                try:
                    buf = self._rb(base, size)
                except Exception:
                    buf = b""
                i = buf.find(needle)
                while i != -1:
                    if i % 8 == 0 and self._is_game(base + i):
                        self.game = base + i
                        return self.game
                    i = buf.find(needle, i + 8)
            a = base + size
        raise GTError("Game instance not found in heap")

    # ---------------- semantic reads ----------------
    def notice_errors(self, db: int, path: str) -> bool | None:
        """FactDef(path).noticeErrors via db.factLib.factDefs (static data, cached)."""
        if path in self._notice:
            return self._notice[path]
        if not self._notice:
            defs = self.string_map(self.u64(self.u64(db + DB_FACTLIB) + FACTLIB_FACTDEFS), keys_stable=True)
            for k, fd in defs.items():
                if k is not None and fd:
                    self._notice[k] = bool(self.u8(fd + FACTDEF_NOTICE_ERRORS))
            self._notice.setdefault("", False)
        return self._notice.get(path)

    def snapshot(self) -> dict:
        t = time.time()
        g = self.find_game()
        s: dict = {"t": t, "ok": True}
        day = self.u64(g + GAME_DAY)
        scr = self.u64(g + GAME_SCREEN)
        s["screen"] = self.obj_class(scr)
        if day:
            s["day"] = self.i32(day + DAY_ID)
            s["day_date_jd"] = jd = self.f64(day + DAY_DATE)
            try:  # Julian day number -> calendar date (day 1 = 1982-11-23)
                s["date"] = (datetime.date(1858, 11, 17) + datetime.timedelta(days=int(jd - 2400000.5))).isoformat()
            except (OverflowError, ValueError):
                s["date"] = None
            s["day_duration_min"] = self.f64(day + DAY_DURATION_MIN)
            s["day_min_travelers"] = self.i32(day + DAY_MIN_TRAVELERS)
            s["day_made"] = self.i32(day + DAY_NUM_MADE)
            s["day_processed_paid"] = self.i32(day + DAY_PAID)
            s["day_processed_unpaid"] = self.i32(day + DAY_UNPAID)
            s["day_processed"] = s["day_processed_paid"] + s["day_processed_unpaid"]
            s["day_detains"] = self.i32(day + DAY_NUM_DETAINS)
            s["day_waiting_line"] = self.i32(day + DAY_WAITING_LINE)
            s["day_bribe_money"] = self.i32(day + DAY_BRIBE)
            cits = []
            for c in self.hx_array(self.u64(day + DAY_CITATIONS)):
                ct = self.u64(c + CIT_TYPE)
                idx = self.i32(ct + ENUM_INDEX) if ct else -1
                cname = self.obj_class(ct) or ""
                typ = cname.split("_", 1)[1] if cname.startswith("CitationType_") else (
                    CITATION_TYPES[idx] if 0 <= idx < 3 else f"?{idx}")
                cits.append({"type": typ, "penalty_cost": self.i32(c + CIT_PENALTY_COST)})
            s["citations"] = cits
            s["num_citations"] = len(cits)
            s["num_penalties"] = sum(1 for c in cits if c["type"] == "PENALTY")
            s["penalty_cost"] = sum(c["penalty_cost"] for c in cits if c["type"] == "PENALTY")
            run = self.u64(day + DAY_RUN)
            db = self.u64(run + DAYRUN_DB) if run else 0
            story = self.u64(run + DAYRUN_STORY) if run else 0
            facts = self.factset(self.u64(story + STORY_FACTS)) if story else {}
            for k, name in STAT_KEYS.items():
                v = facts.get(k)
                try:
                    s[name] = int(v) if v not in (None, "") else None
                except ValueError:
                    s[name] = v
        else:
            db = 0
        # booth (only while a DayScreen is up)
        if s["screen"] == "DayScreen":
            booth = self.u64(scr + DAYSCREEN_BOOTH)
            if booth:
                clk = self.u64(booth + BOOTH_CLOCK)
                s["booth_time"] = self.f64(clk + CLOCK_TIME) if clk else None
                con = self.u64(booth + BOOTH_CONSOLE)
                if con:
                    s["console_traveler_count"] = self.i32(con + CONSOLE_TRAVELER_COUNT)
                    cc = self.u64(con + CONSOLE_CLOCK)
                    if cc:
                        h = self.f64(cc + CONSOLECLOCK_HOUR)
                        s["clock_hour"] = h
                        m = int(h * 60 + 1e-6)
                        s["clock"] = f"{(m // 60) % 24:02d}:{m % 60:02d}"
                eng = self.u64(booth + BOOTH_ENGINE)
                if eng:
                    s.update(self._entrant(eng))
        return s

    def _entrant(self, eng: int) -> dict:
        e = {
            "eng_num_travelers": self.i32(eng + ENG_NUM_TRAVELERS),
            "eng_mistake_check": bool(self.u8(eng + ENG_MISTAKE_CAUGHT)),
            "eng_have_stamped": bool(self.u8(eng + ENG_HAVE_STAMPED)),
            "eng_just_made_mistake": bool(self.u8(eng + ENG_JUST_MADE_MISTAKE)),
        }
        env = self.u64(eng + ENG_ENV)
        trv = self.u64(env + ENV_TRAVELER) if env else 0
        envrun = self.u64(env + ENV_RUN) if env else 0
        db = self.u64(envrun + ENVRUN_DB) if envrun else 0
        if not trv:
            e["entrant"] = None
            return e
        nm = self.u64(trv + TRV_NAME)
        first = self.string_nocache(self.u64(nm + TNAME_FIRST)) if nm else None
        last = self.string_nocache(self.u64(nm + TNAME_LAST)) if nm else None
        invalid = [self.string_nocache(p) for p in self.hx_array(self.u64(env + ENV_INVALID_FACT_PATHS))]
        noticeable = [p for p in invalid if p and db and self.notice_errors(db, p)]
        efacts = self.factset(self.u64(env + ENV_FACTS))
        given_raw = efacts.get("Traveler/Approval")
        given = None
        if given_raw:
            up = given_raw.upper()
            given = "APPROVED" if "APPROVED" in up else "DENIED" if "DENIED" in up else given_raw
        has_err = bool(noticeable)
        e["entrant"] = {
            "name": " ".join(x for x in (first, last) if x) or None,
            "id_number": self.string_nocache(self.u64(trv + TRV_ID_NUMBER)),
            "nationality": self.string_nocache(self.u64(trv + TRV_NATIONALITY)),
            "spec_id": self.string_nocache(self.u64(trv + TRV_SPEC_ID)),
            "force_deny": bool(self.u8(trv + TRV_FORCE_DENY)),
            "detained": bool(self.u8(trv + TRV_DETAINED)),
            "gave_stamped_passport": bool(self.u8(trv + TRV_GAVE_STAMPED_PASSPORT)),
            "invalid_fact_paths": invalid,
            "noticeable_errors": noticeable,
            # BoothEngine.handleEvent cites a mistake iff isApproved(given) == hasErrors(false)
            "correct_verdict": "DENIED" if has_err else "APPROVED",
            "given_verdict": given,
            "given_raw": given_raw,
            "verdict_correct": None if given is None else (given == ("DENIED" if has_err else "APPROVED")),
        }
        return e


_reader: Reader | None = None


def snapshot() -> dict:
    """Module-level convenience: attach lazily, never raise.

    Returns {"ok": False, "error": ...} when the game isn't running / not in
    a readable state.  Safe to call every loop tick.
    """
    global _reader
    try:
        if _reader is None:
            _reader = Reader()
        return _reader.snapshot()
    except Exception as e:  # process gone, mid-GC pointer, scene change...
        msg = f"{type(e).__name__}: {e}"
        if isinstance(e, GTError) or "Could not" in msg or "process" in msg.lower():
            # re-attach next call if the process went away
            try:
                if _reader is not None and not _reader.pm.process_handle:
                    _reader = None
            except Exception:
                _reader = None
        if _reader is not None:
            _reader.game = _reader.game if _reader._is_game(_reader.game) else 0
        return {"t": time.time(), "ok": False, "error": msg}
