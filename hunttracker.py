#!/usr/bin/env python3
"""
Hunt Tracker - a live stats window for Hunt: Showdown 1896.

Shows what you're doing right now (menus / searching / in a hunt with a timer / post-match)
plus stats worked out from the game's own log files: hunts, time hunting, queue times,
death screens, game modes, your last 7 days, and your hunter level.
It only READS the logs. It never touches the game.

    py hunttracker.py              open the tracker window (toggles along its top)
    py hunttracker.py --web        show it in a browser tab instead
    py hunttracker.py --console    text version
    py hunttracker.py --log PATH   use a different Game.log

Plain Python 3.9+, no installs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

FROZEN = getattr(sys, "frozen", False)  # the installed .exe
# "Publish Update.bat" bumps this and uploads the build; installed copies update themselves to it.
VERSION = "1.0.10"
UPDATE_REPO = "xoxoFaith1993/hunt-tracker"
# A one-file .exe unpacks to a temp folder that __file__ points into; the install folder is the exe's.
HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
# The installed .exe keeps its settings and data per user; running the script keeps them beside it.
DATA = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "HuntTracker" if FROZEN else HERE
DATA.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA / "hunttracker.json"
HISTORY_PATH = DATA / "hunttracker_history.json"
HIST_VERSION = 2
DEFAULTS = {
    "log_path": "",           # filled in automatically on first run (or set it yourself)
    "port": 8765,
    "auto_update": True,      # the installed .exe updates itself from GitHub when it opens
    "hidden_families": [],    # weapon families hidden from the Career page (click its heading)
    "host": "127.0.0.1",     # "0.0.0.0" lets a phone/tablet on your Wi-Fi open it (no password!)
    "poll_seconds": 2,
    "window_scale": 1.0,      # old fixed scale; the window now just scales to its size
    "always_on_top": False,   # keep the window above other windows (ON TOP at the top)
    "detailed": True,         # detailed stats view (DETAILED at the top)
    "min_hunt_seconds": 120,  # ignore "hunts" shorter than this (e.g. instant disconnects)
    "quick_leave_seconds": 300,
    # Hunt doesn't save kills anywhere, so you count them with global hotkeys (work while the game has focus).
    # Deaths are counted automatically from death screens; the death keys are only for fixing mistakes.
    "kill_key": "F9",
    "undo_kill_key": "F10",
    "death_key": "F7",
    "undo_death_key": "F8",
    # Saves screenshots of the post-match summary into the "summaries" folder (for reading kills/MMR).
    "save_summary_screens": True,
    # Reads your stats (Windows' built-in OCR) whenever the menu's Statistics / weapon stats page is open.
    "read_stats_page": True,
}
SUMMARY_DIR = DATA / "summaries"
CAREER_PATH = DATA / "hunttracker_career.json"


# Main weapon types don't overlap; the others (Sniper, Silencer, Lever-Action...) are also counted
# inside one of these, so adding them would count kills twice.
BASE_TYPES = ("Rifle", "Shotgun", "Revolver", "Pistol", "Melee", "Bow", "Crossbow")
# Weapon classes Hunt only lists as a family (own class icon, no matching "<type> Kills" stat).
STANDALONE_FAMILIES = ("Crossbow",)
FAMILY_HEAD = "KILLS BY WEAPON FAMILY \u25be"  # click it in the window to pick which families show
# Every weapon family, with the class icon used when it has no icon of its own.
FAMILY_CLASS = {
    **{f: "Rifle" for f in ("1865 Carbine", "Berthier 1892", "Centennial", "Drilling", "Frontier 73C",
                            "Infantry 73L", "Krag", "Lebel 1886", "Mako 1895", "Marathon", "Martini-Henry",
                            "Maynard Sniper", "Mosin-Nagant", "Nitro Express", "Ranger 73", "Sparks",
                            "Springfield 1866", "Vetterli 71")},
    **{f: "Revolver" for f in ("Bornheim No. 3", "Burgess", "Conversion", "Dolch 96", "Haymaker", "LeMat",
                               "Nagant M1895", "New Army", "Officer", "Pax", "Scottfield", "Uppercut")},
    **{f: "Shotgun" for f in ("Auto-5", "Rival 78", "Romero 77", "Slate", "Specter 1882", "Terminus")},
    **{f: "Crossbow" for f in ("Crossbow", "Hunting Bow", "Chu Ko Nu")},
    "Bomb Lance": "Melee",
}


def family_of(name: str):
    """The family a weapon or variant name belongs to ("Crossbow Deadeye" -> "Crossbow"), or None."""
    hits = [f for f in FAMILY_CLASS if name == f or name.startswith(f + " ")]
    return max(hits, key=len) if hits else None


def known_kills(c: dict, extra: int = 0):
    """(total, where it came from, extra): the exact "Players Killed" count from the K/D tooltip when
    it's been read, otherwise the larger of main weapon types or weapon families; plus `extra` kills
    read from match summaries since then."""
    if not c:
        return None, "", 0
    exact = c.get("headline", {}).get("Players Killed")
    if exact is not None:
        return exact + extra, "Players Killed", extra
    types, fams = c.get("weapon_types", {}), c.get("weapon_families", {})
    solo = [k for k in STANDALONE_FAMILIES if k in fams and k not in types]
    by_type = sum(v for k, v in types.items() if k in BASE_TYPES) + sum(fams[k] for k in solo)
    by_family = sum(fams.values())
    if not (by_type or by_family or extra):
        return None, "", 0
    if by_type >= by_family:
        src = " + ".join([k for k in BASE_TYPES if k in types] + solo)
    else:
        src = "weapon families"
    return max(by_type, by_family) + extra, src, extra


HUNTER_KILLS = re.compile(r"hunters?\s*killed\s*\+\s*(\d+)", re.I)


def read_summary_kills(words: list, w: int = 0, h: int = 0, px: bytes = b""):
    """"Hunters Killed +N" on the post-match summary, or None when this frame isn't that page or the
    number can't be read. OCR often drops a small "+5" sitting far from its label, so the label and
    the spot right of it are enlarged and read again together (a lone number gets ignored)."""
    for ws in ocr_lines(words):
        txt = " ".join(q[4] for q in ws)
        if not re.search(r"hunters?\s*killed", txt, re.I):
            continue
        m = HUNTER_KILLS.search(txt)
        if m:
            return int(m.group(1))
        if not px:
            return None
        start = next(q for q in ws if re.match(r"hunters?", q[4], re.I))
        lab = next(q for q in ws if re.match(r"killed", q[4], re.I))
        import tempfile
        crop = Path(tempfile.gettempdir()) / "hunttracker_kills.png"
        write_crop_png(crop, w, h, px, start[0] - 10, lab[1] - lab[3], lab[0] + lab[2] + int(w * 0.12),
                       lab[1] + 2 * lab[3], scale=3)
        m = HUNTER_KILLS.search(" ".join(q[4] for q in ocr_words(crop)))
        return int(m.group(1)) if m else None
    return None


# What the Career page lists before anything has been read, so it shows at 0 instead of blank.
CAREER_OTHER = ["Grand Slam", "Triple Stack", "Honored Contracts", "Wild Targets Killed", "Running the Gauntlet"]
CAREER_TYPES = ["Rifle", "Shotgun", "Revolver", "Melee", "Semi-Automatic", "Silencer",
                "Single-Shot", "Sniper", "Lever-Action", "Marksman", "Pump-Action", "Sawn-Off"]


def load_career():
    """Career stats copied from the in-game profile page (edit hunttracker_career.json to update)."""
    try:
        return json.loads(CAREER_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

TS = re.compile(r"^<(\d\d):(\d\d):(\d\d)>")
HEADER = re.compile(r"Log Started at \w{3} (\w{3})\s+(\d+) (\d\d):(\d\d):(\d\d) (\d{4})")
DEEP = re.compile(r"GotoDeepLinkingTarget\(Lobby, (\w+)\)")
# Screens the tracker reads: career / weaponstats = your Statistics pages in the menu,
# profilecards_showcase = player cards on the loading screen, death = death screen.
WATCH_UI = re.compile(r"(Starting|Stopping) UI state machine '(career|career_statistics|weaponstats|profilecards_showcase|death|bountyhunt|teamdetails|profilecard_statistics)/")
MODES = {"BountyHuntLobby": "Bounty Hunt", "BountyClashLobby": "Bounty Clash"}
MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


# ------------------------------------------------------------------ helpers
def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            print(f"Couldn't read {CONFIG_PATH.name} ({e}); using defaults.")
    else:
        try:
            CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2), encoding="utf-8")
        except OSError:
            pass
    return cfg


def pause() -> None:
    try:
        input("\nPress Enter to close this window...")
    except (EOFError, KeyboardInterrupt, RuntimeError):
        pass


def fail(msg: str) -> None:
    print("\n" + msg)
    if sys.stdin is None:  # launched without a console window: show the error in a dialog instead
        try:
            import tkinter
            from tkinter import messagebox
            r = tkinter.Tk()
            r.withdraw()
            messagebox.showerror("Hunt Tracker", msg)
            r.destroy()
        except Exception:  # noqa: BLE001
            pass
    else:
        pause()
    sys.exit(1)


GAME_DIRS = ("Hunt Showdown 1896", "Hunt Showdown")


def steam_roots() -> list:
    """Every Steam library folder we can find (registry, libraryfolders.vdf, common spots)."""
    roots = []
    try:
        import winreg
        for hive, key, val in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                               (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                               (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    roots.append(Path(winreg.QueryValueEx(k, val)[0]))
            except OSError:
                pass
    except ImportError:
        pass
    for pf in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if pf:
            roots.append(Path(pf) / "Steam")
    for L in "CDEFGHIJ":
        for sub in ("Steam", "SteamLibrary", "Games/Steam", "Games/SteamLibrary", "Program Files (x86)/Steam"):
            roots.append(Path(f"{L}:/") / sub)
    for r in list(roots):  # extra libraries listed by Steam itself
        vdf = r / "steamapps" / "libraryfolders.vdf"
        try:
            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="ignore")):
                roots.append(Path(m.group(1).replace("\\\\", "\\")))
        except OSError:
            pass
    seen, out = set(), []
    for r in roots:
        if str(r).lower() not in seen:
            seen.add(str(r).lower())
            out.append(r)
    return out


def ask_for_log() -> Path | None:
    try:
        import tkinter
        from tkinter import filedialog, messagebox
        r = tkinter.Tk()
        r.withdraw()
        messagebox.showinfo("Hunt Tracker", "Couldn't find Hunt: Showdown automatically.\n\n"
                            "Next, pick your Hunt folder (the one that contains the USER folder), "
                            "e.g. ...\\steamapps\\common\\Hunt Showdown 1896")
        d = filedialog.askdirectory(title="Select your Hunt Showdown folder")
        r.destroy()
        if d:
            for c in (Path(d) / "USER" / "Game.log", Path(d) / "Game.log", Path(d).parent / "Game.log"):
                if c.exists():
                    return c
    except Exception:  # noqa: BLE001
        pass
    return None


def find_log(cfg: dict) -> Path | None:
    if cfg.get("log_path") and Path(cfg["log_path"]).exists():
        return Path(cfg["log_path"])
    for r in steam_roots():
        for game in GAME_DIRS:
            p = r / "steamapps" / "common" / game / "USER" / "Game.log"
            if p.exists():
                return p
    return None


def fmt_dur(sec) -> str:
    sec = max(0, int(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_hm(sec) -> str:
    m = int(max(0, sec) // 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def fmt_ago(sec: float) -> str:
    if sec < 5:
        return "live"
    if sec < 90:
        return f"log {int(sec)}s ago"
    if sec < 3600:
        return f"log {int(sec // 60)}m ago"
    return f"log {int(sec // 3600)}h ago"


def header_epoch(line: str):
    m = HEADER.search(line)
    if not m or m.group(1) not in MONTHS:
        return None
    try:
        return time.mktime((int(m.group(6)), MONTHS[m.group(1)], int(m.group(2)),
                            int(m.group(3)), int(m.group(4)), int(m.group(5)), 0, 0, -1))
    except (ValueError, OverflowError):
        return None


# ------------------------------------------------------------------ log parsing
def parse_log(text: str, min_hunt: int = 120) -> dict:
    """Walk one Game.log. Times are log-clock seconds; each hunt gets a real start time from the header."""
    sid, base, day, raw_prev = None, None, 0, None
    first = last = None
    state, cur, hunts, left_early = "menu", None, [], 0
    ui_open: set = set()
    mode, q_req, q_pending = "Bounty Hunt", None, None
    death_n, revived, revived_at, death_starts = 0, True, None, []
    for line in text.splitlines():
        if sid is None and line.startswith("Log Started at"):
            sid, base = line.strip(), header_epoch(line)
        m = TS.match(line)
        if not m:
            continue
        raw = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        if raw_prev is not None and raw < raw_prev - 3600:
            day += 1  # passed midnight in the log's clock
        raw_prev = raw
        t = raw + day * 86400
        first = t if first is None else first
        last = t
        body = line[m.end():].strip()

        sm = WATCH_UI.match(body)
        if sm:
            (ui_open.add if sm.group(1) == "Starting" else ui_open.discard)(sm.group(2))

        if cur is not None:
            if "Starting UI state machine 'death/" in body:
                cur["deaths"] += 1
                # A death screen is a new death only if you got back up since the last one (the game
                # fades back in when you're revived); otherwise it's the same screen reopened.
                # Spectating a teammate also fades, but the death screen is back within a second or two;
                # after a real revive it takes a while to get shot again.
                if revived and (revived_at is None or t - revived_at >= 5):
                    death_n = death_n + 1
                    death_starts.append((t, death_n))
                revived = False
            elif "layout 'fader'" in body and "unloading" in body and not revived:
                revived, revived_at = True, t
            elif "User requested to leave mission" in body:
                cur["left"] = True

        if body.startswith("CMetaMissionBag state:"):
            st = body.split(":", 1)[1].strip()
            if st == "MissionStarted":
                if cur is None:
                    cur = {"start": t, "mode": mode, "deaths": 0, "q": q_pending, "left": False}
                    q_pending, death_n, revived, revived_at = None, 0, True, None
                state = "hunt"
            elif st == "ContentsDumped":
                if cur is not None:
                    d = t - cur["start"]
                    if d >= min_hunt:
                        hunts.append({"d": d, "t": (base + (cur["start"] - first)) if base else None,
                                      "mode": cur["mode"], "deaths": cur["deaths"], "q": cur["q"], "left": cur["left"]})
                    cur = None
                state = "post"
            elif st == "Empty":
                if cur is not None:  # mission ended without a normal wrap-up
                    left_early += 1
                    cur = None
                if state != "hunt":
                    state = "menu"
        elif state != "hunt":
            dm = DEEP.search(body)
            if dm and dm.group(1) in MODES:
                mode = MODES[dm.group(1)]
            if "CMatchMaking::Request response" in body:
                state, q_req = "search", t
            elif "CMatchMaking::OnResult" in body and "err = 0" in body:
                state = "found"
                if q_req is not None and 0 <= t - q_req <= 1800:
                    q_pending = t - q_req
                q_req = None
            elif "CGameLobby::DoSetReadiness" in body and "ready=1" in body and state in ("menu", "search", "found"):
                state = "found"
            elif "Starting UI state machine 'post_match_summary" in body:
                state = "post"
            elif "Stopping UI state machine 'post_match_summary" in body:
                state = "menu"
    return {"sid": sid, "base": base, "state": state, "start": cur["start"] if cur else None,
            "mode": cur["mode"] if cur else None,
            "hunt_t": (base + (cur["start"] - first)) if (cur and base) else None,
            "cur_deaths": cur["deaths"] if cur else 0,
            "first": first, "last": last, "hunts": hunts, "left_early": left_early,
            "death_n": death_n,  # which real death of this hunt the death screen belongs to
            "death_starts": [((base + (dt_ - first)) if base else None, n) for dt_, n in death_starts],
            "stats_open": bool(ui_open & {"career", "career_statistics", "weaponstats"}),
            "cards_open": "profilecards_showcase" in ui_open, "death_open": "death" in ui_open,
            "lobby_open": "bountyhunt" in ui_open,  # the team lobby (hunters with names over them)
            "details_open": "teamdetails" in ui_open,  # post-match Team Details: every player's MMR
            "cardstats_open": "profilecard_statistics" in ui_open,  # Player Profile > pick a card statistic
            "weapons_open": "weaponstats" in ui_open,  # the weapon kills list
            "play": (last - first) if (first is not None and last is not None) else 0}


def read_level(log_path: Path):
    f = log_path.parent / "Profiles" / "default" / "attributes.xml"
    try:
        m = re.search(r'name="Unlocks/UnlockRank"\s+value="(\d+)"', f.read_text(encoding="utf-8", errors="replace"))
        return int(m.group(1)) if m else None
    except OSError:
        return None


def hunt_running() -> bool:
    if os.name != "nt":
        return True
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq HuntGame.exe", "/NH"], capture_output=True, text=True,
                             timeout=5, creationflags=0x08000000).stdout
        return "HuntGame.exe" in out
    except Exception:  # noqa: BLE001
        return True


# ------------------------------------------------------------------ summary screenshots (Windows only, no extra installs)
def hunt_is_foreground() -> bool:
    import ctypes
    from ctypes import wintypes
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        buf, size = ctypes.create_unicode_buffer(512), wintypes.DWORD(512)
        ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return bool(ok) and buf.value.lower().endswith("huntgame.exe")
    finally:
        k32.CloseHandle(h)


def grab_foreground():
    """(width, height, BGRA bytes) of the foreground window, in real pixels."""
    import ctypes
    from ctypes import wintypes
    u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32
    try:  # per-thread DPI awareness, so the tkinter window's scaling is untouched
        old = u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except AttributeError:
        old = None
    try:
        r = wintypes.RECT()
        u32.GetWindowRect(u32.GetForegroundWindow(), ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w <= 0 or h <= 0:
            return None
        sdc = u32.GetDC(None)
        mdc = g32.CreateCompatibleDC(sdc)
        bmp = g32.CreateCompatibleBitmap(sdc, w, h)
        g32.SelectObject(mdc, bmp)
        g32.BitBlt(mdc, 0, 0, w, h, sdc, r.left, r.top, 0x00CC0020)  # SRCCOPY

        class BIH(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]
        bih = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)  # negative height = top-down rows
        data = ctypes.create_string_buffer(w * h * 4)
        g32.GetDIBits(mdc, bmp, 0, h, data, ctypes.byref(bih), 0)
        g32.DeleteObject(bmp)
        g32.DeleteDC(mdc)
        u32.ReleaseDC(None, sdc)
        return w, h, data.raw
    finally:
        if old:
            u32.SetThreadDpiAwarenessContext(old)


def write_png(path: Path, w: int, h: int, bgra: bytes) -> None:
    import struct
    import zlib
    rgb = bytearray(w * h * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    stride = w * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(h))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def write_crop_png(path: Path, w: int, h: int, bgra: bytes, x0: int, y0: int, x1: int, y1: int,
                   scale: int = 4, repeat: int = 1) -> None:
    """A region as a big grey image with dark text on white, which OCR reads far better than
    small light text on a dark background. repeat > 1 lays copies side by side ("0  0  0"):
    OCR skips a short number that's alone, but reads a row of them."""
    import struct
    import zlib
    x0, x1, y0, y1 = max(0, x0), min(w, x1), max(0, y0), min(h, y1)
    # Copies need wide white gaps to read as separate numbers; a single crop gets none (a wide image
    # can pass Windows OCR's size limit and come back empty).
    gap = b"\xff" * (max(12, (x1 - x0) * 2) * scale) if repeat > 1 else b""
    rows = []
    for y in range(y0, y1):
        row = bgra[(y * w + x0) * 4:(y * w + x1) * 4]
        grey = bytes(255 - max(row[i], row[i + 1], row[i + 2]) for i in range(0, len(row), 4))
        wide = bytes(v for v in grey for _ in range(scale))
        rows += [b"\x00" + gap + (wide + gap) * repeat] * scale
    cw, ch = len(gap) + ((x1 - x0) * scale + len(gap)) * repeat, (y1 - y0) * scale

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", cw, ch, 8, 0, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + chunk(b"IEND", b""))


def read_png(path: Path):
    """(w, h, BGRA bytes) from a PNG this program wrote (RGB, no row filters)."""
    import struct
    import zlib
    data = path.read_bytes()
    w, h = struct.unpack(">II", data[16:24])
    pos, idat = 8, b""
    while pos < len(data):
        n = struct.unpack(">I", data[pos:pos + 4])[0]
        if data[pos + 4:pos + 8] == b"IDAT":
            idat += data[pos + 8:pos + 8 + n]
        pos += 12 + n
    raw = zlib.decompress(idat)
    rgb = b"".join(raw[y * (w * 3 + 1) + 1:(y + 1) * (w * 3 + 1)] for y in range(h))
    bgra = bytearray(w * h * 4)
    bgra[0::4], bgra[1::4], bgra[2::4] = rgb[2::3], rgb[1::3], rgb[0::3]
    return w, h, bytes(bgra)


OCR_PS1 = r'''param([string]$Path)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8  # names can have symbols like †
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$t) { $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); $task.Wait(-1) | Out-Null; $task.Result }
[void][Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$eng = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
$res = Await ($eng.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
foreach ($l in $res.Lines) { foreach ($w in $l.Words) {
    $r = $w.BoundingRect
    [string]::Join('|', @([int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height, $w.Text))
} }
'''


# The same OCR as OCR_PS1, but kept running: it reads one image path per line and answers with the
# words and an end marker. Starting PowerShell takes about a second; a running one answers in a blink.
OCR_SERVER_PS1 = OCR_PS1.replace("param([string]$Path)\n", "").split("$file = Await")[0] + r'''
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
$eng = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
while ($null -ne ($Path = [Console]::In.ReadLine())) {
  try {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $res = Await ($eng.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
    foreach ($l in $res.Lines) { foreach ($w in $l.Words) {
      $r = $w.BoundingRect
      [Console]::Out.WriteLine([string]::Join('|', @([int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height, $w.Text)))
    } }
    $stream.Dispose()
  } catch { }
  [Console]::Out.WriteLine('<<END>>')
  [Console]::Out.Flush()
}
'''
_ocr = {"proc": None, "lock": threading.Lock()}


def _ocr_server(png: Path):
    """Ask the long-running OCR process; None if it can't be used (then the one-off way is used)."""
    import tempfile
    with _ocr["lock"]:
        p = _ocr["proc"]
        if p is None or p.poll() is not None:
            script = Path(tempfile.gettempdir()) / "hunttracker_ocr_server.ps1"
            if not script.exists() or script.read_text(encoding="utf-8") != OCR_SERVER_PS1:
                script.write_text(OCR_SERVER_PS1, encoding="utf-8")
            p = _ocr["proc"] = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                encoding="utf-8", errors="replace", bufsize=1, creationflags=0x08000000)
        try:
            p.stdin.write(str(png) + "\n")
            p.stdin.flush()
            out = []
            for ln in p.stdout:
                ln = ln.rstrip("\r\n")
                if ln == "<<END>>":
                    return "\n".join(out)
                out.append(ln)
        except (OSError, ValueError):
            pass
        _ocr["proc"] = None  # it died: start a fresh one next time
        return None


def ocr_words(png: Path) -> list:
    """Windows' built-in OCR (Windows 10/11) -> [(x, y, w, h, text)]."""
    import tempfile
    # The running OCR process can keep an image file open for a while, which would block the next
    # write to it; so it gets its own copy each time, and old copies are cleared out as they free up.
    import shutil
    inbox = Path(tempfile.gettempdir()) / "hunttracker_ocr_in"
    inbox.mkdir(exist_ok=True)
    _ocr["n"] = _ocr.get("n", 0) + 1
    copy = inbox / f"{os.getpid()}_{_ocr['n']}.png"
    shutil.copyfile(png, copy)
    out = _ocr_server(copy)
    for old in inbox.glob("*.png"):
        if old != copy:
            try:
                old.unlink()
            except OSError:
                pass  # still open: next time
    if out is None:
        script = Path(tempfile.gettempdir()) / "hunttracker_ocr.ps1"
        if not script.exists() or script.read_text(encoding="utf-8") != OCR_PS1:
            script.write_text(OCR_PS1, encoding="utf-8")
        out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), str(png)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                             creationflags=0x08000000).stdout or ""
    words = []
    for ln in out.splitlines():
        p = ln.split("|", 4)
        if len(p) == 5:
            words.append((int(p[0]), int(p[1]), int(p[2]), int(p[3]), p[4]))
    return words


NUM = re.compile(r"^\d[\d,]*(\.\d+)?$")
LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 '/&.\-]{2,40}$")  # "1865 Carbine Family Kills"


def stat_rows(words: list) -> list:
    """Group OCR words into 'Label ... number' rows. Only rows whose label lines up with the
    stat column are kept, which drops the player and hunter name lines above it."""
    rows: list = []
    for wd in sorted(words, key=lambda q: q[1]):
        for r in rows:
            if abs(r[0] - wd[1]) <= max(8, wd[3] * 0.6):
                r[1].append(wd)
                break
        else:
            rows.append([wd[1], [wd]])
    found = []
    for _, ws in rows:
        ws.sort(key=lambda q: q[0])
        if len(ws) < 2 or not NUM.match(ws[-1][4]):
            continue
        val, prev = ws[-1], ws[-2]
        if val[0] - (prev[0] + prev[2]) < 3 * max(val[3], 10):  # the number sits far right of its label
            continue
        label = " ".join(q[4] for q in ws[:-1])
        if LABEL.match(label):
            raw = val[4].replace(",", "")
            found.append((ws[0][0], label, float(raw) if "." in raw else int(raw)))
    if not found:
        return []
    xs = sorted(x for x, _, _ in found)
    col = xs[len(xs) // 2]
    return [(lab, v) for x, lab, v in found if abs(x - col) <= 25]


HEADLINE = {"Kills/Deaths": "K/D", "KDA Ratio": "KDA", "Kills/Deaths/Assists": "KDA",  # the Progression page's name
            "Headshots": "Headshots", "Teams Wiped": "Teams Wiped",
            "Total Bounty": "Total Bounty", "Monsters Killed": "Monsters Killed",
            "Boss Targets Killed": "Boss Targets Killed",
            # From the tooltip on the K/D stat: exact counts ("Players Killed: 968").
            "Players Killed": "Players Killed", "Killed by Player": "Killed by Player",
            "Kill Assists": "Kill Assists"}
KD_TOOLTIP = re.compile(r"(Players Killed|Killed by Player|Kill Assists)\s*:\s*([\d,]+)", re.I)


HUNTER_MOST = "Hunter With Most Kills: "  # + the hunter's name, as read_profile_stat returns it


def read_profile_stat(words: list, w: int, h: int, px: bytes, png: Path):
    """Player Profile > Statistic: the hovered stat's name is the title on the left; the card on
    the right shows its value in the top slot. -> (name, value) or None."""
    left = ocr_lines([q for q in words if q[0] < w * 0.4 and q[1] < h * 0.4])
    texts = [" ".join(q[4] for q in ln) for ln in left]
    i = next((k for k, t_ in enumerate(texts) if re.fullmatch(r"Statistic\s*\d+", t_)), None)
    if i is None or i + 1 >= len(texts):
        return None
    name = _clean(texts[i + 1])
    card = [ln for ln in ocr_lines([q for q in words if q[0] > w * 0.65 and h * 0.7 < q[1] < h * 0.9])
            if re.search("[A-Za-z]{3}", " ".join(q[4] for q in ln))]
    if not card or not name:
        return None
    top = card[0]  # the card's first slot shows the hovered stat
    label = " ".join(q[4] for q in top if not NUM.match(q[4]))
    short = re.sub(r"\s+K(?:i|il|ill|ills)?$", "", label)
    if name.lower().startswith("variant with most") and family_of(short):
        name = short + " Kills"  # the card names the variant
    elif name.lower().startswith("hunter with most"):
        # The card names the hunter ("Lilith Kills 318").
        if short == label or not re.fullmatch(r"[A-Z][\w'.-]+( [A-Z][\w'.-]+)?", short) or family_of(short):
            return None  # the card hasn't caught up with the hover yet
        name = HUNTER_MOST + short
    elif player_key(label)[:6] != player_key(name)[:6]:
        return None  # the card hasn't caught up with the hover yet
    if NUM.match(top[-1][4]):
        raw = top[-1][4].replace(",", "")
        return name, float(raw) if "." in raw else int(raw)
    # A small lone number ("0") that OCR skipped: read the spot right of the label on its own.
    x0, x1 = top[-1][0] + top[-1][2] + 4, int(w * 0.9)
    val = _ocr_number(png, w, h, px, x0, top[0][1] - 6, x1, top[0][1] + top[0][3] + 8)
    return (name, val) if val is not None else None


def read_kd_tooltip(words: list) -> list:
    """The K/D stat's hover tooltip on the Statistics page -> [(label, number)]."""
    found = []
    for ws in ocr_lines(words):
        for m in KD_TOOLTIP.finditer(" ".join(q[4] for q in ws)):
            label = next(k for k in HEADLINE if k.lower() == m.group(1).lower())
            found.append((label, int(m.group(2).replace(",", ""))))
    return found


def note_deltas(c: dict, old: dict) -> None:
    """Remember how much each stat went up since it was last read (shown as a green +N until the
    next match starts)."""
    d = c.setdefault("deltas", {})
    for sec in ("headline", "other", "weapon_types", "weapon_families", "profile_stats"):
        for k, v in c.get(sec, {}).items():
            was = old.get(sec, {}).get(k)
            if isinstance(v, int) and isinstance(was, int) and v > was:
                d[k] = d.get(k, 0) + v - was


def merge_stats(rows: list, weapons_page: bool = False, picker: bool = False, only_missing: bool = False) -> bool:
    """Write OCR'd stats into hunttracker_career.json. Returns True if anything changed.
    weapons_page: the rows come from the weapon kills list, the only place a new weapon type can come
    from. Elsewhere (the profile card beside your Statistics) "<name> Kills" that isn't a known weapon
    type is a hunter's kills ("Lilith Kills 316"). picker: one stat hovered in Profile > Statistic
    (never a hunter). only_missing: fill gaps only, never overwrite a number already there."""
    if not rows:
        return False
    c = load_career() or {}
    before = json.dumps(c, sort_keys=True)
    hunters = {h.lower() for h in c.get("hunter_kills", {})}
    types = {k.lower() for k in CAREER_TYPES + ["Carbine"] + list(c.get("weapon_types", {}))} - hunters
    # A hunter's kills read cut short before this fix ("Lilith K" in the other stats): file them properly.
    for k, v in c.get("other", {}).items():
        m = re.fullmatch(r"(.+?)\s+K(?:i|il|ill|ills)?", k)
        if m and isinstance(v, int) and m.group(1) not in c.get("hunter_kills", {}):
            c.setdefault("hunter_kills", {})[m.group(1)] = {"n": v, "match": time.time()}

    def put(sec, key, val):
        if not (only_missing and key in c.get(sec, {})):
            c.setdefault(sec, {})[key] = val

    # "Crossbow Deadeye" was once filed as a weapon type: it's your variant with the most kills.
    for k in [k for k in c.get("weapon_types", {}) if family_of(k) and family_of(k) != k]:
        v_ = c["weapon_types"].pop(k)
        c.setdefault("top_variant", {"name": k, "n": v_})

    for label, val in rows:
        label = re.sub(r"\s*/\s*", "/", label)  # OCR spaces slashes unevenly: "Kills/ Deaths/ Assists"
        label = re.sub(r"\s+K(?:i|il|ill|ills)?$", " Kills", label)  # OCR cuts it short: "Lilith K"
        if not re.search(r"[A-Za-z]{3}", label):
            continue  # not a stat name ("e 45 77" from some other screen)
        name = label[:-len(" Kills")]
        fam = family_of(name) if label.endswith(" Kills") and not label.endswith(" Family Kills") else None
        if label in HEADLINE:
            put("headline", HEADLINE[label], val)
        elif fam and fam == name:  # a family listed without "Family" ("Sparks Kills")
            put("weapon_families", name, val)
        elif fam:  # a variant ("Crossbow Deadeye Kills"): Variant With Most Kills, on your profile card
            if not (only_missing and c.get("top_variant")):
                c["top_variant"] = {"name": name, "n": val}
        elif label.endswith(" Family Kills"):
            put("weapon_families", label[:-len(" Family Kills")], val)
        elif label.endswith(" Kills") and (name.lower() in types or (weapons_page and name.lower() not in hunters)):
            put("weapon_types", name, val)
        elif label.endswith(" Kills") and not picker and isinstance(val, int) and re.fullmatch(r"[A-Z][\w'.-]+( [A-Z][\w'.-]+)?", name):
            hk = c.setdefault("hunter_kills", {})
            if hk.get(name, {}).get("n") != val:
                hk[name] = {"n": val, "match": time.time()}  # matches after this add to it
        elif label in CAREER_OTHER or label == "Maxed-Out Hunters":
            put("other", label, val)
    # Anything else that slipped into "other" before (menu text read as a stat, "My Team").
    if "other" in c:
        c["other"] = {k: v for k, v in c["other"].items() if k in CAREER_OTHER or k == "Maxed-Out Hunters"}
    for k in ("other", "weapon_types", "weapon_families"):
        if k in c:
            c[k] = dict(sorted(c[k].items(), key=lambda kv: -kv[1]))
    if json.dumps(c, sort_keys=True) == before:
        return False
    old = json.loads(before)
    note_deltas(c, old)
    if any(old.get(k) != c.get(k) for k in ("weapon_types", "weapon_families")):
        c["weapons_at"] = time.time()  # fresh weapon totals already include earlier summary kills
    if old.get("headline", {}).get("Players Killed") != c.get("headline", {}).get("Players Killed"):
        c["kills_at"] = time.time()  # same for the exact kill count
    c.pop("summary_kills", None)
    c["updated"] = dt.date.today().isoformat()
    tmp = CAREER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, indent=2), encoding="utf-8")
    tmp.replace(CAREER_PATH)
    return True


def ocr_lines(words: list) -> list:
    """OCR words -> rows of words on the same line, top to bottom: [[(x, y, w, h, text), ...], ...]."""
    rows: list = []
    for wd in sorted(words, key=lambda q: q[1]):
        for r in rows:
            if abs(r[0] - wd[1]) <= max(8, wd[3] * 0.6):
                r[1].append(wd)
                break
        else:
            rows.append([wd[1], [wd]])
    return [sorted(ws, key=lambda q: q[0]) for _, ws in rows]


def encounters(p: dict) -> int:
    """Matches you've seen a player in: on your loading cards, or anywhere in Team Details."""
    return len(set(p.get("matches", [])) | set(p.get("seen", [])))


def name_score(name: str):
    """Prefer spellings without OCR's stray spaces, then ones that kept their underscores etc."""
    return (-name.count(" "), len(name))


# Shown for a squad mate whose squad stats page you've opened.
PAGE_STATS = ("KDA Ratio", "Teams Wiped")
# Which of a player's card stats to show first in Player Summary.
STAT_ORDER = ("Kills/Deaths", "KDA Ratio", "Teams Wiped", "Total Bounty", "Headshots", "Maxed-Out Hunters")
def read_columns(words: list, height: int) -> list:
    """Side-by-side player columns (loading cards, or the lobby's squad stats):
    names in the top row, each player's "label ... number" stats below.
    -> [{"name", "stats", "x"}], left to right."""
    words = [w for w in words if w[1] < height * 0.86 and re.search("[0-9A-Za-z]", w[4])]  # no bottom status text
    lines = ocr_lines(words)
    is_num = lambda seg: all(NUM.match(q[4]) for q in seg)  # noqa: E731

    def segments(ws):
        """Split a line at wide gaps, then glue each lone number back onto the label before it."""
        segs = [[ws[0]]]
        for a, b in zip(ws, ws[1:]):
            if b[0] - (a[0] + a[2]) > 2.5 * max(a[3], 10):
                segs.append([])
            segs[-1].append(b)
        out = []
        for s in segs:
            if out and is_num(s) and len(s) == 1 and not is_num(out[-1]) and not NUM.match(out[-1][-1][4]):
                out[-1] = out[-1] + s
            else:
                out.append(s)
        return out

    stat_segs = []  # (x, y, label, value)
    for ws in lines:
        for s in segments(ws):
            if len(s) >= 2 and NUM.match(s[-1][4]):
                lw = s[:-1]
                while len(lw) > 1 and len(lw[0][4]) == 1:  # a stat's icon read as a stray letter ("e Teams Wiped")
                    lw = lw[1:]
                label = " ".join(q[4] for q in lw)
                if LABEL.match(label):
                    raw = s[-1][4].replace(",", "")
                    stat_segs.append((lw[0][0], s[0][1], label, float(raw) if "." in raw else int(raw)))
    if not stat_segs:
        return []
    # Each player is one column of stats; columns are far apart.
    gap = height * 0.15
    col_x: list = []
    for x in sorted(s[0] for s in stat_segs):
        if not col_x or x - col_x[-1][-1] > gap:
            col_x.append([x])
        else:
            col_x[-1].append(x)
    col_x = [xs[0] for xs in col_x]
    # The name row is the topmost line with one name lined up over every column and no lone numbers,
    # which skips the menu bar ("3 100 14645 ..."), and the prestige / hunter rows under the names.
    # A name ending in a number ("Spartan 65") also looks like a stat, so search above the middle of the stats.
    slack = height * 0.13
    mid_y = sorted(s[1] for s in stat_segs)[len(stat_segs) // 2]
    names = None
    for ws in lines:
        if ws[0][1] >= mid_y:
            break
        segs = segments(ws)
        if any(is_num(s) for s in segs):
            continue
        picked = []
        for cx in col_x:
            near = [s for s in segs if abs(s[0][0] - cx) < slack]
            if not near:
                break
            picked.append(min(near, key=lambda s: abs(s[0][0] - cx)))
        if len(picked) == len(col_x):
            # Every name on this row, including players whose stats are hidden (they have no column).
            names = [s for s in segs if len(" ".join(q[4] for q in s)) >= 2]
            break
    if not names:
        return []
    cols = [{"name": trim_name(s), "x": s[0][0], "stats": {}} for s in names]
    name_y = names[0][0][1] + names[0][0][3]
    # Lobby: under the names comes a row of prestige / rank numbers, then the hunter row ("Lilith 0"),
    # which looks like a stat. Cards have badge numbers there and a title row with no numbers instead.
    i = next(k for k, ws in enumerate(lines) if ws[0][1] == names[0][0][1] or names[0][0] in ws)
    hunter_y = None
    if i + 2 < len(lines) and all(NUM.match(q[4]) for q in lines[i + 1]):
        hunter_y = lines[i + 2][0][1]
    for x, y, label, val in stat_segs:
        if y <= name_y or (hunter_y is not None and abs(y - hunter_y) <= 8):
            continue
        min(cols, key=lambda c: abs(c["x"] - x))["stats"][label] = val
    for c in cols:
        c["hidden"] = not c["stats"]  # the player set their stats to hidden
    return cols


LOOKALIKE = str.maketrans({"0": "o", "1": "l", "i": "l", "|": "l", "5": "s"})


def player_key(name: str) -> str:
    """How player names are matched. OCR reads "Spartan65" as "Spartan 65", "Lic_Fede777" as
    "Lic Fede777" and "MikkoBellic" as "Mikk0Be11ic", so spacing, symbols, accents and look-alike
    characters don't count."""
    import unicodedata
    plain = unicodedata.normalize("NFKD", name.lower()).encode("ascii", "ignore").decode()
    return re.sub(r"[^0-9a-z]", "", plain.translate(LOOKALIKE))


def count_stars(px: bytes, w: int, x0: int, x1: int, y0: int, y1: int):
    """(filled, total) MMR stars in a box: bright white stars are earned, grey ones aren't."""
    cols = []
    for x in range(max(0, x0), x1):
        cols.append(max(max(px[(y * w + x) * 4:(y * w + x) * 4 + 3]) for y in range(max(0, y0), y1)))
    blobs, cur = [], []
    for v in cols + [0]:
        if v > 75:
            cur.append(v)
        elif cur:
            if len(cur) >= 3:
                blobs.append(max(cur))
            cur = []
    return sum(1 for b in blobs if b > 175), len(blobs)


# What Team Details shows instead of a name when it's hidden (Hide Player Names, or the player's setting).
HIDDEN_NAME = re.compile(r"^\W*$|unknown|hidden|^hunter\s*\d*$|^player\s*\d*$|\?{2,}|\*{2,}"
                         r"|(?:^|\s)[tT1l]?\s*eam\s*\d+$", re.I)  # also a misread "Team 3" header


def read_team_details(words: list, w: int, h: int, px: bytes) -> list:
    """Post-match Team Details -> [(name, stars, team number)] for every fully visible row.
    A row is "<name> ... MMR ★★★★☆☆"; "Team N" headers split the teams."""
    out, team, first_header = [], None, None
    lines = ocr_lines([q for q in words if re.search("[0-9A-Za-z]", q[4])])
    labels = [q for ln in lines for q in ln if q[4] == "MMR"
              and not any(o[4] in ("Team", "MATCH") for o in ln)]
    names_x = [ln[0][0] for ln in lines if any(q in labels for q in ln)]
    for ln in lines:
        txt = [q[4] for q in ln]
        if txt and txt[0] == "Team" and len(txt) > 1 and txt[1].isdigit():
            team = int(txt[1])
            first_header = first_header or team
            continue
        if any(t in ("Team", "MATCH", "MATCHMMR") for t in txt):
            continue
        mmr = next((q for q in ln if q[4] == "MMR"), None)
        if not mmr and labels and any(abs(ln[0][0] - x) < 40 for x in names_x):
            # OCR skipped this row's "MMR" label: the stars sit in the same column as the other rows'.
            ref = labels[0]
            mmr = (ref[0], ln[0][1] + (ln[0][3] - ref[3]) // 2, ref[2], ref[3], "MMR")
        if not mmr:
            continue
        name = trim_name([q for q in ln if q[0] + q[2] < mmr[0] - 20])
        if len(name) < 2 or mmr[1] + 2 * mmr[3] > h or HIDDEN_NAME.search(name):
            continue  # only players whose name is actually shown
        sx = mmr[0] + mmr[2] + 4
        filled, total = count_stars(px, w, sx, sx + int(w * 0.11), mmr[1] - mmr[3] // 3, mmr[1] + mmr[3] + mmr[3] // 3)
        if total == 6:  # a row cut off at the edge, or something covering it, isn't trusted
            out.append((name, filled, team))
    # Rows above the first header on screen (scrolled down) belong to the team before that header.
    if first_header and first_header > 1:
        out = [(n, s, first_header - 1 if t_ is None else t_) for n, s, t_ in out]
    return out


def trim_name(seg: list) -> str:
    """A name from OCR words, minus icon junk at either end ("†" read as "t", "•e'")."""
    def junk(word):  # a capital or digit on its own is a real part of the name ("N osRaze1")
        core = re.sub("[^0-9A-Za-z]", "", word)
        return len(core) <= 1 and not re.fullmatch("[A-Z0-9]", core)

    while len(seg) > 1 and junk(seg[0][4]):
        seg = seg[1:]
    while len(seg) > 1 and junk(seg[-1][4]):
        seg = seg[:-1]
    return _clean(" ".join(q[4] for q in seg))


DEATH_SKIP = re.compile(r"hide|show|revive|profile|player names|spectate|back to menu", re.I)


def _clean(txt: str) -> str:
    """Drop OCR'd icon junk around a name: control characters and symbols at either end."""
    txt = re.sub(r"[\x00-\x1f]", "", txt)
    return re.sub(r"^[^0-9A-Za-z\[(]+|[^0-9A-Za-z_\])]+$", "", txt).strip()


def read_death(words: list, width: int, height: int) -> dict:
    """A death screen -> {"killer", "hunter", "weapon", "dist", "lines"}.
    Right side: "Killed by" and the killer's hunter near the top, the killer's player name low down
    (about 3/4 of the way, over their prestige / rank). Left side: "Killed with" and the weapon."""
    lines = [" ".join(q[4] for q in ws) for ws in ocr_lines(words)]
    right = ocr_lines([w for w in words if w[0] > width * 0.72 and w[1] < height * 0.9])
    hunter = killer = weapon = None
    for i, ws in enumerate(right):
        txt = " ".join(q[4] for q in ws)
        if re.search(r"killed\s*by", txt, re.I):
            rest = _clean(re.split(r"killed\s*by\s*:?", txt, flags=re.I)[-1])
            nxt = _clean(" ".join(q[4] for q in right[i + 1])) if i + 1 < len(right) else ""
            hunter = rest or nxt or None
            break
    # Only the name plate counts: a teammate's voice-chat bubble sits much higher on the right.
    for ws in right:
        if not height * 0.72 <= ws[0][1] <= height * 0.82:
            continue
        name = trim_name(ws)
        if len(re.sub("[^0-9A-Za-z]", "", name)) >= 2 and not DEATH_SKIP.search(name) and len(name) <= 32 \
                and re.search("[A-Za-z]", name):
            killer = name
    left = [_clean(" ".join(q[4] for q in ws)) for ws in ocr_lines([w for w in words if w[0] < width * 0.4])]
    for i, txt in enumerate(left):
        if re.search(r"killed\s*with", txt, re.I):
            parts = []
            for nxt in left[i + 1:i + 3]:
                if not nxt or re.search(r"distance|related|traits", nxt, re.I):
                    break
                parts.append(nxt)
            weapon = " ".join(parts) or None
            break
    dist = None
    for i, txt in enumerate(lines):  # "Distance to killer:" with the "8m" on the same line or just under it
        if re.search(r"distance\s*to\s*killer", txt, re.I):
            for nxt in [txt.split(":", 1)[-1]] + lines[i + 1:i + 3]:
                m = re.search(r"\b(\d{1,4})\s*m\b", nxt)
                if m:
                    dist = int(m.group(1))
                    break
            break
    return {"killer": killer, "hunter": hunter, "weapon": weapon, "dist": dist, "lines": lines}


MISSION_LEFT = re.compile(r"ends\s*in\s*:?\s*(\d{1,2})\s*[:.]\s*(\d{2})", re.I)
DIGITISH = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "l": "1", "I": "1", "|": "1", "S": "5",
                          "s": "5", "B": "8", "Z": "2", "z": "2", ",": "", ".": ""})


def _ocr_number(png: Path, w: int, h: int, px: bytes, x0: int, y0: int, x1: int, y1: int):
    """One short number, read as three copies side by side (a lone number gets skipped by OCR)."""
    write_crop_png(png, w, h, px, x0, y0, x1, y1, scale=3, repeat=3)
    reads = [q[4].translate(DIGITISH) for q in ocr_words(png)]
    reads = [r for r in reads if r.isdigit()]
    return int(max(set(reads), key=reads.count)) if reads else None


def read_menu_money(png: Path, w: int, h: int, px: bytes):
    """(Hunt Dollars, Blood Bonds) from the menu's top-left bar: prestige, rank, dollars, bonds, "+"."""
    write_crop_png(png, w, h, px, 0, 0, int(w * 0.2), int(h * 0.06), scale=3)
    nums = [q[4].translate(DIGITISH) for q in ocr_words(png)]
    nums = [int(n) for n in nums if n.isdigit()]
    return (nums[-2], nums[-1]) if len(nums) >= 3 else None


def read_match_money(png: Path, w: int, h: int, px: bytes):
    """(Hunt Dollars, Blood Bonds) earned so far, top-left of the map: a white coin icon and white
    number, then an orange bond icon and orange number. Found by colour, then each number read alone."""
    rw, rh = int(w * 0.2), int(h * 0.08)

    def kind(x):
        white = orange = 0
        for y in range(rh):
            i = (y * w + x) * 4
            b, g, r = px[i], px[i + 1], px[i + 2]
            if min(r, g, b) > 190 and max(r, g, b) - min(r, g, b) < 45:
                white += 1
            elif r > 190 and 120 < g < 200 and b < 165 and r - b > 55:
                orange += 1
        return "W" if white >= 2 else "O" if orange >= 2 else "."

    runs: list = []  # [kind, x0, x1]
    for x in range(rw):
        k = kind(x)
        if k == ".":
            continue
        if runs and runs[-1][0] == k and x - runs[-1][2] <= 2:
            runs[-1][2] = x
        else:
            runs.append([k, x, x])
    whites = [r for r in runs if r[0] == "W"]
    oranges = [r for r in runs if r[0] == "O" and whites and r[1] > whites[0][2]]
    if len(whites) < 2 or len(oranges) < 2:
        return None
    dollars_x = (whites[1][1], max((r[2] for r in whites[1:] if r[2] < oranges[0][1]), default=whites[1][2]))
    bonds_x = (oranges[1][1], oranges[-1][2])

    def rows(a, b):  # the number's own height, so the crop is just the digits
        ys = [y for y in range(rh) for x in range(a, b + 1, 2)
              if px[(y * w + x) * 4 + 2] > 190]
        return (min(ys), max(ys)) if ys else (0, rh)

    vals = []
    for a, b in (dollars_x, bonds_x):
        y0, y1 = rows(a, b)
        vals.append(_ocr_number(png, w, h, px, a - 3, y0 - 4, b + 4, y1 + 5))
    return tuple(vals) if None not in vals else None


def start_map_timer(t: "Tracker") -> None:
    """Holding Tab in a hunt opens the map, which shows "MISSION ENDS IN 42:11". The timer only shows
    whole seconds, so one read puts the end somewhere in a 1-second window; it keeps reading while Tab
    is held, narrowing that window, until it's pinned to within a quarter second.
    Only Tab's up/down state is checked (GetAsyncKeyState); nothing touches the game."""
    if os.name != "nt":
        return
    import ctypes
    import tempfile
    png = Path(tempfile.gettempdir()) / "hunttracker_map.png"

    def run():
        money_at, debug_hunt = 0.0, None
        while True:
            time.sleep(0.15)
            try:
                cur = t.cur or {}
                hunt = cur.get("hunt_t")
                if cur.get("state") != "hunt" or not hunt:
                    continue
                m = t.mission if t.mission.get("hunt") == hunt else None
                need_timer = not (m and m["hi"] - m["lo"] <= 0.25)  # not pinned down yet
                need_money = time.time() - money_at > 3  # money earned this match: keep it fresh
                if not (need_timer or need_money):
                    continue
                if not ctypes.windll.user32.GetAsyncKeyState(0x09) & 0x8000 or not hunt_is_foreground():
                    continue
                t0 = time.time()
                img = grab_foreground()
                t1 = time.time()
                if not img:
                    continue
                w, h, px = img
                if need_timer:  # the timer comes first, so it syncs as soon as the map is up
                    # Just the timer strip across the top: much quicker to read than the whole map.
                    write_crop_png(png, w, h, px, int(w * 0.15), 0, int(w * 0.85), int(h * 0.2), scale=2)
                    hit = MISSION_LEFT.search(" ".join(q[4] for q in ocr_words(png)))
                    if not hit:
                        continue  # the map isn't up yet (the Objectives panel comes first)
                    lo = (t0 + t1) / 2 + int(hit.group(1)) * 60 + int(hit.group(2))  # "39:47" = 39:47.0-39:47.99
                    hi = lo + 1
                    if m and max(lo, m["lo"]) < min(hi, m["hi"]):
                        lo, hi = max(lo, m["lo"]), min(hi, m["hi"])
                    t.mission = t.hist["mission"] = {"hunt": hunt, "lo": lo, "hi": hi, "end": (lo + hi) / 2}
                    t.changed_at = time.time()
                    t._save()  # survives a tracker restart mid-hunt
                    if hi - lo > 0.25:
                        continue  # keep narrowing the timer before spending time on the money
                if need_money:
                    money_at = time.time()
                    try:  # its own try: a failed money read must never stop the timer reads
                        earned = read_match_money(Path(tempfile.gettempdir()) / "hunttracker_money.png", w, h, px)
                    except Exception:  # noqa: BLE001
                        earned = None
                    if earned:
                        t.record_earned(*earned)
                    elif hunt != debug_hunt:  # keep one failed screen per hunt to see what went wrong
                        debug_hunt = hunt
                        (SUMMARY_DIR / "debug").mkdir(parents=True, exist_ok=True)
                        write_png(SUMMARY_DIR / "debug" / "map_money_failed.png", w, h, px)
            except Exception:  # noqa: BLE001
                continue

    threading.Thread(target=run, daemon=True).start()


def start_summary_capture(t: "Tracker") -> None:
    """While Hunt has focus: save each new post-match summary screen, read the Statistics /
    weapon stats page into the Career stats, and read loading cards / death screens into the player log."""
    if os.name != "nt":
        return
    import tempfile
    stats_png = Path(tempfile.gettempdir()) / "hunttracker_stats.png"

    lobby_png = Path(tempfile.gettempdir()) / "hunttracker_lobby.png"
    wallet_png = Path(tempfile.gettempdir()) / "hunttracker_wallet.png"
    err_log = DATA / "capture_errors.log"

    def run():
        last_sig, shots, lobby_at, wallet_at = None, 0, 0.0, 0.0
        was = {"cards": False, "death": False}
        while True:
            time.sleep(0.5)  # death screens only last a few seconds; stats pages change as you scroll
            try:
                cur = t.cur or {}
                # Your Hunt Dollars / Blood Bonds from the menu's top-left bar, now and then.
                if (cur.get("state") in ("menu", "search") and not cur.get("details_open")
                        and time.time() - wallet_at > 20 and hunt_is_foreground()):
                    wallet_at = time.time()
                    img = grab_foreground()
                    money = read_menu_money(wallet_png, *img) if img else None
                    if money:
                        t.record_wallet(*money)
                summary = cur.get("state") == "post" and t.cfg.get("save_summary_screens", True)
                cardstats = bool(cur.get("cardstats_open"))  # Player Profile > Statistic: one per hover
                stats = (cur.get("stats_open") or cardstats) and t.cfg.get("read_stats_page", True)
                cards, death = bool(cur.get("cards_open")), bool(cur.get("death_open"))
                # The squad stats page, while searching or once a match is found (it needs 5+ stats per
                # column to count, so other menu screens don't get read as players).
                lobby = cur.get("state") in ("search", "found") and not cards and time.time() - lobby_at > 6
                if cards and not was["cards"]:
                    t.new_match()
                dk = (t.hist["matches"][-1]["key"] if t.hist["matches"] else 0, cur.get("death_n"))
                if death and dk != was.get("dk"):
                    t.new_death(cur.get("death_n") or 0)
                    was["dk"] = dk
                was["cards"], was["death"] = cards, death
                folder = ("loading_cards" if cards else "death" if death
                          else "team_details" if cur.get("details_open") else None)
                if not (summary or stats or folder or lobby):
                    last_sig, shots = None, 0
                    continue
                if shots >= 40 and not stats:  # caps saved screenshots; stats pages are read every time
                    continue
                if not hunt_is_foreground():
                    m = t.hist["matches"][-1] if t.hist["matches"] else {}
                    if (cards and not m.get("cards")) or (death and not t.death_read()):
                        t.capture_note = f"missed {folder.replace('_', ' ')}: Hunt wasn't the active window"
                    continue
                img = grab_foreground()
                if not img:
                    continue
                w, h, px = img
                sig = hash(px[::4093])  # cheap "did the screen change" check
                if sig == last_sig:
                    continue
                last_sig = sig
                if lobby and not (summary or stats or folder):
                    lobby_at = time.time()
                    write_png(lobby_png, w, h, px)
                    lw = ocr_words(lobby_png)
                    # Only the squad stats screen: the 3D team lobby turned banners and text into fake names.
                    cols = read_columns(lw, h)
                    if cols and max(len(c["stats"]) for c in cols) >= 5:
                        t.record_players(cols, "lobby", "")
                    continue
                shots += 1
                if summary or folder:
                    out = SUMMARY_DIR / folder if folder else SUMMARY_DIR
                    out.mkdir(parents=True, exist_ok=True)
                    png = out / f"{time.strftime('%Y-%m-%d_%H-%M-%S')}.png"
                    write_png(png, w, h, px)
                    t.capture_note = ""
                    if not folder:
                        k = read_summary_kills(ocr_words(png), w, h, px)
                        if k is not None:
                            t.record_summary_kills(k)
                    elif folder == "loading_cards":
                        cols = read_columns(ocr_words(png), h)
                        if cols:
                            t.record_players(cols, "cards", png.name)
                    elif folder == "death":
                        t.record_death(read_death(ocr_words(png), w, h), png.name)
                    elif folder == "team_details":  # every scroll position: whichever rows are on screen
                        rows = read_team_details(ocr_words(png), w, h, px)
                        if rows:
                            t.record_mmrs(rows)
                else:
                    time.sleep(0.4)  # let the page finish its fade-in, then read a settled frame
                    img = grab_foreground()
                    if img:
                        write_png(stats_png, *img)
                        sw = ocr_words(stats_png)
                        if cardstats:
                            got = read_profile_stat(sw, *img, Path(tempfile.gettempdir()) / "hunttracker_card.png")
                            if got:
                                t.record_profile_stat(*got)
                        # The page's stat rows, plus the K/D hover tooltip's exact kills / deaths / assists.
                        elif merge_stats(stat_rows(sw) + read_kd_tooltip(sw), bool(cur.get("weapons_open"))):
                            t.changed_at = time.time()
            except Exception:  # noqa: BLE001 - never let a screenshot problem stop the tracker
                import traceback
                try:
                    old = err_log.read_text(encoding="utf-8")[-20000:] if err_log.exists() else ""
                    err_log.write_text(old + time.strftime("\n[%Y-%m-%d %H:%M:%S]\n") + traceback.format_exc(),
                                       encoding="utf-8")
                except OSError:
                    pass

    threading.Thread(target=run, daemon=True).start()


VK = {**{f"F{i}": 0x6F + i for i in range(1, 25)}, **{f"NUM{i}": 0x60 + i for i in range(10)},
      "INSERT": 0x2D, "DELETE": 0x2E, "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22,
      "PAUSE": 0x13, "SCROLLLOCK": 0x91}
MOD_KEYS = {"ALT": 1, "CTRL": 2, "SHIFT": 4}


def parse_hotkey(s: str):
    """'F9' / 'ctrl+K' / 'NUM5' -> (modifiers, virtual key), or None if empty/unknown."""
    mods, vk = 0, None
    for part in (s or "").upper().replace(" ", "").split("+"):
        if part in MOD_KEYS:
            mods |= MOD_KEYS[part]
        elif part in VK:
            vk = VK[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part)
        elif part:
            return None
    return (mods, vk) if vk else None


def start_hotkeys(t: "Tracker") -> None:
    """System-wide hotkeys (RegisterHotKey: no hooks, nothing touches the game). Errors go to t.hotkey_errors."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    actions = [("kill_key", lambda: t.add_kill(1)), ("undo_kill_key", lambda: t.add_kill(-1)),
               ("death_key", lambda: t.add_death(1)), ("undo_death_key", lambda: t.add_death(-1))]

    def run():
        binds, hid = {}, 0
        for cfg_key, fn in actions:
            name = t.cfg.get(cfg_key, "")
            hk = parse_hotkey(name)
            if not hk:
                if name:
                    t.hotkey_errors.append(f"{name}?")
                continue
            mods, vk = hk
            # with no modifier given, also catch it while Shift (sprint) / Ctrl (crouch) / Alt are held
            combos = [mods] if mods else range(8)
            ok = False
            for m in combos:
                hid += 1
                if user32.RegisterHotKey(None, hid, m | 0x4000, vk):  # 0x4000 = MOD_NOREPEAT
                    binds[hid], ok = fn, True
            if not ok:
                t.hotkey_errors.append(name)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312 and msg.wParam in binds:  # WM_HOTKEY
                binds[msg.wParam]()

    threading.Thread(target=run, daemon=True).start()


# ------------------------------------------------------------------ tracker
class Tracker:
    def __init__(self, cfg: dict, log: Path):
        self.cfg, self.log = cfg, log
        self.hist = self._load()
        self.sig, self.cur, self.mtime = None, None, 0.0
        self.running, self.running_at = True, 0.0
        self.level = read_level(log)
        self.changed_at = 0.0
        self.lock = threading.Lock()
        self.hotkey_errors: list = []
        self.hist.setdefault("kills", [])   # wall-clock time of each kill hotkey press
        self.hist.setdefault("dadj", [])    # [time, +1/-1] manual death corrections
        self.hist.setdefault("mmr", [])     # [time, stars 1-6], newest last
        self.hist.setdefault("players", {})  # key -> {"name", "matches": [keys], "stats", "hunter", "weapon", "first", "last"}
        self.hist.setdefault("matches", [])  # newest last: {"key", "cards": [names], "killers": [names]}
        self.hist.setdefault("reads", [])    # last raw screen reads, for checking what OCR saw
        self.mission: dict = self.hist.get("mission", {})  # {"hunt": hunt start, "end": epoch}, read off the map
        if not self.hist.get("kd_since"):   # K/D only counts from when kill tracking started
            self.hist["kd_since"] = time.time()
        self._seed_backups()
        self._note_level()

    def _load(self) -> dict:
        try:
            h = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            if h.get("v") == HIST_VERSION:
                return h
        except (OSError, ValueError):
            pass
        return {"v": HIST_VERSION, "sessions": {}, "seeded": False, "levels": []}

    # ---- player log (filled from loading cards and death screens)
    def _key(self, name: str) -> str:
        """The player-log key for a name: an already-logged player whose name is nearly the same
        (OCR misread a letter or two), unless the numbers in the names differ."""
        import difflib
        k = player_key(name)
        players = self.hist.get("players", {})
        if k in players or len(k) < 8:
            return k
        digits = re.sub(r"\D", "", k)
        best, score = None, 0.0
        for other in players:
            if re.sub(r"\D", "", other) != digits:
                continue
            r = difflib.SequenceMatcher(None, k, other).ratio()
            if r > score:
                best, score = other, r
        return best if score >= 0.85 else k

    def _me(self) -> str:
        """Your own player name: learned from the middle loading card, else the career file."""
        return player_key(self.cfg.get("player_name") or (load_career() or {}).get("name") or "")

    def _learn_me(self, cols: list) -> None:
        """Your card is always the middle one (loading cards and the squad stats screen). With 1 or 3
        players that's unambiguous; with 2 there's no middle, so wait for another match."""
        if self.cfg.get("player_name") or len(cols) not in (1, 3):
            return
        self.cfg["player_name"] = sorted(cols, key=lambda c: c["x"])[len(cols) // 2]["name"]
        try:
            CONFIG_PATH.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _player(self, name: str, rough: bool = False) -> dict:
        """rough: the name came from the team lobby, where OCR is least reliable."""
        now = time.time()
        p = self.hist["players"].setdefault(self._key(name), {"name": name, "matches": [], "stats": {},
                                                              "first": now, "rough_name": rough})
        if (p.get("rough_name") and not rough) or (rough == p.get("rough_name", False)
                                                   and name_score(name) > name_score(p["name"])):
            p["name"], p["rough_name"] = name, rough  # keep the cleanest spelling seen
        p["last"] = now
        return p

    def record_players(self, cols: list, kind: str, file: str) -> None:
        """Players read from the loading cards ("cards") or the lobby's squad stats ("lobby")."""
        self._read(kind, {"name": ", ".join(c["name"] for c in cols), "players": cols}, file)
        self._learn_me(cols)
        me = self._me()
        m = self.hist["matches"][-1] if self.hist["matches"] else None
        if kind == "lobby":
            self.hist["squad"] = {"t": time.time(),
                                  "names": [c["name"] for c in cols if self._key(c["name"]) != me]}
        for c in cols:
            if self._key(c["name"]) == me:
                if kind != "lobby" and m is not None:
                    self._note_hunter_kills(c["stats"], m)
                self.note_my_stats(c["stats"])
                continue
            p = self._player(c["name"])
            # Squad-page stats and card stats are kept apart: a card never overwrites the page.
            if kind == "lobby":
                p.setdefault("page_stats", {}).update(c["stats"])
            elif c["stats"]:
                p["card_stats"] = c["stats"]
            p["stats"] = {**p.get("card_stats", {}), **p.get("page_stats", {})}
            p["hidden"] = bool(c.get("hidden")) and not p["stats"]
            if kind == "lobby":
                p["squad"] = True
            elif m is not None:
                if m["key"] not in p["matches"]:
                    p["matches"].append(m["key"])
                if self._key(c["name"]) not in [self._key(n) for n in m["cards"]]:
                    m["cards"].append(c["name"])
        self.changed_at = time.time()
        self._save()

    def note_my_stats(self, stats: dict) -> None:
        """Your own column on the squad stats page (or your loading card) into your Career stats.
        Monsters Killed is left out: that page counts it differently from your Statistics page.
        A count never goes down, so a misread lower number is ignored."""
        c = load_career() or {}
        rows = []
        for label, val in stats.items():
            if label == "Monsters Killed" or not isinstance(val, (int, float)):
                continue
            if label in HEADLINE:
                old = c.get("headline", {}).get(HEADLINE[label])
            elif label in CAREER_OTHER or label == "Maxed-Out Hunters":
                old = c.get("other", {}).get(label)
            else:
                continue  # weapon and hunter stats on a card have their own readers
            if isinstance(val, int) and isinstance(old, int) and val < old:
                continue
            rows.append((label, val))
        if rows:
            old_c = json.loads(json.dumps(c))
            if merge_stats(rows, picker=True):
                new_c = load_career() or {}
                note_deltas(new_c, old_c)
                new_c["updated"] = dt.date.today().isoformat()
                tmp = CAREER_PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps(new_c, indent=2), encoding="utf-8")
                tmp.replace(CAREER_PATH)
                self.changed_at = time.time()

    def _read(self, kind: str, data: dict, file: str) -> None:
        self.hist["reads"] = (self.hist["reads"] + [{"t": time.time(), "kind": kind, "file": file, **data}])[-20:]

    def new_match(self) -> None:
        self.hist["matches"] = (self.hist["matches"] + [{"key": time.time(), "cards": [], "killers": []}])[-100:]
        c = load_career()
        if c and c.pop("deltas", None) is not None:  # the green +N notes last until the next match
            CAREER_PATH.write_text(json.dumps(c, indent=2), encoding="utf-8")
        self.capture_note = ""
        self.changed_at = time.time()
        self._save()

    def _note_hunter_kills(self, stats: dict, m: dict) -> None:
        """Your loading card can show "<Hunter> Kills" (e.g. "Lilith Kills 301"): kills with the hunter
        you're playing, as of the start of this match. Weapon stats on the card look the same, so
        anything that's a weapon type or family is skipped."""
        if isinstance(stats.get("Furthest Kill Distance"), int):
            self.record_profile_stat("Furthest Kill Distance", stats["Furthest Kill Distance"])
        c = load_career() or {}
        weapons = {k.lower() for k in list(c.get("weapon_types", {})) + list(c.get("weapon_families", {}))}
        for label, val in stats.items():
            hm = re.fullmatch(r"(.+?) K(?:i|il|ill|ills)?", label)  # OCR often cuts it to "Lilith K"
            if not hm or not isinstance(val, int) or hm.group(1).lower() in weapons or "deadeye" in label.lower():
                continue
            hunter = hm.group(1)
            hk = c.setdefault("hunter_kills", {})
            if hk.get(hunter, {}).get("n") != val or hk.get(hunter, {}).get("match") != m["key"]:
                hk[hunter] = {"n": val, "match": m["key"]}
                tmp = CAREER_PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps(c, indent=2), encoding="utf-8")
                tmp.replace(CAREER_PATH)
            m["hunter"] = hunter

    def hunter_kills(self):
        """(hunter, kills) for your hunter with the most kills. Each hunter's count comes from the last
        loading card showing it, plus "Hunters Killed" from that match's summary and later matches
        played with the same hunter."""
        hk = (load_career() or {}).get("hunter_kills", {})
        if not hk:
            return None
        def total(hunter, rec):
            return rec["n"] + sum(m.get("kills", 0) for m in self.hist["matches"]
                                  if m["key"] >= rec.get("match", 0) and m.get("hunter") == hunter)
        return max(((h, total(h, r)) for h, r in hk.items()), key=lambda hn: hn[1])

    def record_mmrs(self, rows: list) -> None:
        """MMR stars from Team Details. Everyone's is kept quietly and only shown when you're matched
        with that player again; your own row updates your MMR."""
        me = self._me()
        mine = next((team for name, _, team in rows if self._key(name) == me), None)
        if mine is None and self.hist["matches"]:  # scrolled past your own team
            mine = self.hist["matches"][-1].get("my_team")
        now, changed = time.time(), False
        for name, stars, team in rows:
            if self._key(name) == me:
                if not self.hist["mmr"] or self.hist["mmr"][-1][1] != stars:
                    self.hist["mmr"].append([now, stars])
                    changed = True
                continue
            p = self._player(name)
            if p.get("mmr") != stars:
                changed = True
            p["mmr"], p["mmr_at"] = stars, now
            if self.hist["matches"]:  # met in this match, on your team or against you
                key = self.hist["matches"][-1]["key"]
                if key not in p.setdefault("seen", []):
                    p["seen"].append(key)
            if mine is not None and team is not None:
                p["mmr_side"] = "teammate" if team == mine else "enemy"
        if self.hist["matches"]:
            m = self.hist["matches"][-1]
            m["mmr_teams"] = sorted(set(m.get("mmr_teams", [])) | {t for _, _, t in rows if t is not None})
            if mine is not None:
                m["my_team"] = mine
        if changed:
            self.changed_at = now
        self._save()

    def record_profile_stat(self, name: str, value) -> None:
        """A Player Profile card statistic (the hovered one in Profile > Statistic)."""
        c = load_career() or {}
        if name.startswith(HUNTER_MOST) and isinstance(value, int):
            # Your hunter with the most kills: the "<HUNTER> KILLS" row on the tracker. Matches after
            # this add their kills on top until it's read again.
            hunter = name[len(HUNTER_MOST):]
            hk = c.setdefault("hunter_kills", {})
            if hk.get(hunter, {}).get("n") != value:
                hk[hunter] = {"n": value, "match": time.time()}
                c.setdefault("profile_stats", {})["Hunter With Most Kills"] = value
                tmp = CAREER_PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps(c, indent=2), encoding="utf-8")
                tmp.replace(CAREER_PATH)
                self.changed_at = time.time()
            return
        if c.get("profile_stats", {}).get(name) == value:
            if merge_stats([(name, value)], picker=True):
                self.changed_at = time.time()
            return
        old = json.loads(json.dumps(c))
        c.setdefault("profile_stats", {})[name] = value
        note_deltas(c, old)
        tmp = CAREER_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(c, indent=2), encoding="utf-8")
        tmp.replace(CAREER_PATH)
        merge_stats([(name, value)], picker=True)  # and into its place on the Career page (K/D, a weapon, a family...)
        self.changed_at = time.time()

    def record_wallet(self, dollars: int, bonds: int) -> None:
        """Your real totals, read from the menu bar."""
        w = self.hist.get("wallet") or {}
        if (w.get("dollars"), w.get("bonds")) != (dollars, bonds) or time.time() - w.get("t", 0) > 600:
            self.hist["wallet"] = {"dollars": dollars, "bonds": bonds, "t": time.time()}
            if (w.get("dollars"), w.get("bonds")) != (dollars, bonds):
                # Every change, with what the last match picked up and whether you died in it, to check
                # whether a death loses the match's earnings.
                m = self.hist["matches"][-1] if self.hist["matches"] else {}
                died = bool((self.cur or {}).get("hunts")) and (self.cur["hunts"][-1].get("deaths", 0) > 0)
                self.hist["wallet_log"] = (self.hist.get("wallet_log", []) + [{
                    "t": time.time(), "dollars": dollars, "bonds": bonds, "was": [w.get("dollars"), w.get("bonds")],
                    "earned": m.get("earned"), "died": died}])[-200:]
            self.changed_at = time.time()
            self._save()

    def record_earned(self, dollars: int, bonds: int) -> None:
        """What you've picked up this match, read off the map (top left)."""
        if not self.hist["matches"]:
            return
        m = self.hist["matches"][-1]
        if (m.get("earned") or {}).get("dollars") != dollars or m["earned"].get("bonds") != bonds:
            m["earned"] = {"dollars": dollars, "bonds": bonds, "t": time.time()}
            self.changed_at = time.time()
            self._save()

    def wallet_view(self):
        """(dollars, bonds, pending dollars, pending bonds): your last menu total, plus this match's
        earnings until the menu shows the new total."""
        w = self.hist.get("wallet")
        if not w:
            return None
        m = self.hist["matches"][-1] if self.hist["matches"] else {}
        e = m.get("earned") or {}
        if e and e.get("t", 0) > w.get("t", 0):
            return w["dollars"], w["bonds"], e.get("dollars", 0), e.get("bonds", 0)
        return w["dollars"], w["bonds"], 0, 0

    def record_summary_kills(self, kills: int) -> None:
        """"Hunters Killed +N" from this match's summary; every frame shows the same number."""
        if not self.hist["matches"]:
            return
        m = self.hist["matches"][-1]
        if kills > m.get("kills", -1):
            m["kills"] = kills
            self.changed_at = time.time()
            self._save()

    def summary_kills(self) -> int:
        """Kills from match summaries the career numbers don't include yet."""
        c = load_career() or {}
        since = c.get("kills_at") if "Players Killed" in c.get("headline", {}) else c.get("weapons_at", 0)
        since = since or 0
        return sum(m.get("kills", 0) for m in self.hist["matches"] if m["key"] > since)

    def new_death(self, n: int) -> None:
        """n = which real death of this match (a reopened death screen keeps the same n)."""
        m = self.hist["matches"][-1] if self.hist["matches"] else {"key": 0}
        self.death_key = f"{round(m['key'])}-{n}"

    def death_read(self) -> bool:
        m = self.hist["matches"][-1] if self.hist["matches"] else {}
        return getattr(self, "death_key", None) in m.get("deaths", {})

    def record_death(self, death: dict, file: str) -> None:
        """Each frame of a death screen votes for a killer name; the most-read name wins, which
        outvotes a frame where OCR mangled the name."""
        self._read("death", death, file)
        killer = death.get("killer")
        if not killer or not self.hist["matches"] or not getattr(self, "death_key", None):
            return
        m = self.hist["matches"][-1]
        mates = m["cards"] + (self.hist.get("squad") or {}).get("names", [])
        if self._key(killer) in [self._key(n) for n in mates]:
            return  # a teammate's name on screen (voice chat), not who killed you
        votes = m.setdefault("deaths", {}).setdefault(self.death_key, {})
        v = votes.setdefault(self._key(killer), {"n": 0, "name": killer})
        v["n"] += 1
        if name_score(killer) > name_score(v["name"]):
            v["name"] = killer
        v["hunter"], v["weapon"] = death.get("hunter") or v.get("hunter"), death.get("weapon") or v.get("weapon")
        self._tally(m)
        self.changed_at = time.time()
        self._save()

    def _tally(self, m: dict) -> None:
        """This match's killers: each real death's winning name. The same player twice means they
        killed you, you got revived, and they killed you again."""
        winners = []
        for votes in m.get("deaths", {}).values():
            if votes:
                v = max(votes.values(), key=lambda v: v["n"])
                p = self._player(v["name"])  # logged first, so a misread spelling matches the same player
                p["hunter"], p["weapon"] = v.get("hunter"), v.get("weapon")
                winners.append(p["name"])
        m["killers"] = winners
        # Drop players that only existed as a misread killer name that lost the vote.
        keep = {self._key(k) for mm in self.hist["matches"] for k in mm.get("killers", [])}
        for k in [k for k, p in self.hist["players"].items()
                  if not p.get("matches") and not p.get("squad") and p.get("mmr") is None and k not in keep]:
            del self.hist["players"][k]

    def players_view(self) -> dict:
        players, matches = self.hist["players"], self.hist["matches"]
        cur = matches[-1] if matches else None
        sq = self.hist.get("squad")
        # Squad stats read while searching come before the match's loading cards; show whichever is newer.
        squad_mode = bool(sq and sq["names"] and (cur is None or sq["t"] > cur["key"]
                                                  or (not cur["cards"] and cur["key"] - sq["t"] < 600)))
        this = []
        for n in (sq["names"] if squad_mode else cur["cards"] if cur else []):
            p = players.get(self._key(n), {})
            page = p.get("page_stats", {})
            if any(k in page for k in PAGE_STATS):  # from their squad stats page: KDA and Teams Wiped
                shown = {k: page[k] for k in PAGE_STATS if k in page}
            else:  # didn't open it: their loading card's stats
                st = p.get("card_stats") or p.get("stats", {})
                shown = {k: st[k] for k in [k for k in STAT_ORDER if k in st] + [k for k in st if k not in STAT_ORDER]}
            this.append({"name": p.get("name", n), "seen": encounters(p),
                         "stats": shown, "hidden": p.get("hidden", False),
                         "mmr": p.get("mmr")})  # MMR from an earlier match's Team Details
        kills: dict = {}
        for mm in matches:
            for k in mm.get("killers", []):
                kills[self._key(k)] = kills.get(self._key(k), 0) + 1
        # This match's killers only (it clears each match); the count is every time they've killed you.
        killers = []
        if cur and not (squad_mode and sq["t"] > cur["key"]):
            for k in dict.fromkeys(self._key(x) for x in cur.get("killers", [])):
                p = players.get(k, {})
                killers.append((p.get("name", k), kills.get(k, 1), p.get("weapon")))
        often = sorted(((p.get("name", n), encounters(p), p.get("last", 0)) for n, p in players.items()
                        if encounters(p) > 1), key=lambda x: (-x[1], -x[2]))
        last_read = self.hist["reads"][-1] if self.hist["reads"] else None
        new_search = bool(squad_mode and cur and sq["t"] > cur["key"])
        # Once a match's summary has been seen, nag until other teams' MMRs are read too.
        warn = bool(cur and ("kills" in cur or cur.get("mmr_teams"))
                    and not any(t != cur.get("my_team") for t in cur.get("mmr_teams", [])))
        return {"this": this, "squad_mode": squad_mode, "mmr_warning": warn,
                "this_killers": cur["killers"] if (cur and not new_search) else [],
                "match_at": cur["key"] if cur else None,
                "killers": killers, "often": often, "n_players": len(players), "n_matches": len(matches),
                "last_read": last_read, "note": getattr(self, "capture_note", "")}

    def _mission_left(self, cur: dict, state: str, now: float) -> dict:
        """Seconds until the mission ends. Exact once the map's timer has been read this hunt;
        otherwise an estimate: the mission clock starts ~40 s after the log's hunt start (loading in)."""
        hunt = cur.get("hunt_t")
        if state != "hunt" or not hunt:
            return {"mission_left": None, "mission_exact": False}
        if self.mission.get("hunt") == hunt:
            return {"mission_left": max(0.0, self.mission["end"] - now), "mission_exact": True}
        length = (20 if cur.get("mode") == "Bounty Clash" else 45) * 60
        return {"mission_left": max(0.0, hunt + 40 + length - now), "mission_exact": False}

    def _steam(self):
        """(seconds played all-time, last-played epoch) from Steam, re-read once a minute (every 10 s
        until it has been read). A failed read keeps the last good value."""
        now = time.time()
        val = getattr(self, "_steam_val", None)
        if now - getattr(self, "_steam_at", 0) > (60 if val else 10):
            self._steam_at = now
            self._steam_val = steam_playtime(self.log) or val
        return self._steam_val

    def _save(self) -> None:
        with self.lock:
            try:
                tmp = HISTORY_PATH.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.hist), encoding="utf-8")
                tmp.replace(HISTORY_PATH)
            except OSError:
                pass

    def add_kill(self, n: int) -> None:
        if n > 0:
            self.hist["kills"].append(time.time())
        elif self.hist["kills"]:
            self.hist["kills"].pop()
        self.changed_at = time.time()
        self._save()

    def add_death(self, n: int) -> None:
        self.hist["dadj"].append([time.time(), n])
        self.changed_at = time.time()
        self._save()

    def _kd(self, cur: dict, state: str, hunts: list) -> dict:
        now, since = time.time(), self.hist["kd_since"]
        kills, dadj = self.hist["kills"], self.hist["dadj"]

        def count(a, b):  # kills and deaths between two wall-clock times (b=None: up to now)
            b = now + 1 if b is None else b
            return (sum(1 for k in kills if a <= k < b), sum(n for x, n in dadj if a <= x < b))

        live = None
        hunt_t = cur.get("hunt_t")
        if state == "hunt" and hunt_t:
            k, adj = count(hunt_t, None)
            live = (k, max(0, cur.get("cur_deaths", 0) + adj))
        done = [h for h in hunts if h.get("t") and h["t"] >= since]
        last = None
        if done:
            h = max(done, key=lambda x: x["t"])
            k, adj = count(h["t"], h["t"] + h["d"] + 120)  # a little slack for a late key press
            last = (k, max(0, h.get("deaths", 0) + adj))
        deaths = sum(h.get("deaths", 0) for h in done) + sum(n for x, n in dadj if x >= since)
        if hunt_t and hunt_t >= since and state != "offline":
            deaths += cur.get("cur_deaths", 0)
        deaths = max(0, deaths)
        k_total = sum(1 for k in kills if k >= since)
        return {"live_kd": live, "last_kd": last, "kills": k_total, "deaths": deaths,
                "kdr": k_total / deaths if deaths else float(k_total), "kd_since": dt.date.fromtimestamp(since),
                "hotkey_errors": list(self.hotkey_errors),
                "keys": (self.cfg.get("kill_key", ""), self.cfg.get("undo_kill_key", ""))}

    def _note_level(self) -> None:
        if self.level and (not self.hist["levels"] or self.hist["levels"][-1][1] != self.level):
            self.hist["levels"].append([time.time(), self.level])
            self._save()

    def _store(self, info: dict) -> bool:
        if not info["sid"]:
            return False
        rec = {"b": info["base"], "play": info["play"], "hunts": info["hunts"]}
        old = self.hist["sessions"].get(info["sid"])
        self.hist["sessions"][info["sid"]] = rec
        return old is None or len(old.get("hunts", [])) != len(info["hunts"])

    def _seed_backups(self) -> None:
        """Count hunts from Hunt's log backups that haven't been read yet, so days played with the
        tracker closed still show up."""
        folder = self.log.parent / "LogBackups"
        files = sorted(f for f in folder.glob("*.log") if f.is_file()) if folder.exists() else []
        done = set(self.hist.get("backups_read", []))
        new = [f for f in files if f.name not in done]
        if not new:
            return
        print(f"Reading {len(new)} older logs to count your past hunts (a few seconds)...")
        for f in new:
            try:
                self._store(parse_log(f.read_text(encoding="utf-8", errors="replace"), self.cfg["min_hunt_seconds"]))
            except OSError:
                continue
            done.add(f.name)
        self.hist["backups_read"] = sorted(done)
        self.hist["seeded"] = True
        self._save()

    def poll(self) -> bool:
        now = time.time()
        if now - self.running_at > 6:
            self.running, self.running_at = hunt_running(), now
        try:
            st = self.log.stat()
        except OSError:
            return False
        sig = (st.st_mtime, st.st_size)
        if sig == self.sig:
            return False
        self.sig, self.mtime = sig, st.st_mtime
        try:
            info = parse_log(self.log.read_text(encoding="utf-8", errors="replace"), self.cfg["min_hunt_seconds"])
        except OSError:
            return False
        self.cur = info
        if self._store(info):
            self._save()
        self.level = read_level(self.log) or self.level
        self._note_level()
        self.changed_at = now
        return True

    # ---- everything the display needs
    def view(self) -> dict:
        now = time.time()
        cur = self.cur or {"state": "menu", "hunts": [], "start": None, "last": None, "first": None}
        sessions = self.hist["sessions"]
        hunts = [h for s in sessions.values() for h in s.get("hunts", [])]
        durs = [h["d"] for h in hunts]
        state = cur["state"] if self.running else "offline"
        live_extra = max(0.0, min(90.0, now - self.mtime)) if self.mtime else 0.0
        hunt_elapsed = None
        if state == "hunt" and cur["start"] is not None and cur["last"] is not None:
            hunt_elapsed = (cur["last"] - cur["start"]) + live_extra
        session_len = None
        if self.running and cur.get("first") is not None and cur.get("last") is not None:
            session_len = (cur["last"] - cur["first"]) + max(0.0, now - self.mtime)

        today = dt.date.today()
        by_day: dict = {}
        for h in hunts:
            if h.get("t"):
                by_day.setdefault(dt.date.fromtimestamp(h["t"]), []).append(h)
        today_h = by_day.get(today, [])
        week = [((today - dt.timedelta(days=i)), len(by_day.get(today - dt.timedelta(days=i), []))) for i in range(6, -1, -1)]
        hours = day_hours(sessions)
        week_hours = [(day, hours.get(day, 0.0)) for day, _ in week]
        steam = self._steam()
        busiest = max(by_day.items(), key=lambda kv: len(kv[1])) if by_day else None
        first_day = min(by_day) if by_day else None
        qs = [h["q"] for h in hunts if h.get("q") is not None]
        quick = [h for h in hunts if h["d"] < self.cfg["quick_leave_seconds"]]
        nodeath = [h for h in hunts if h.get("deaths", 0) == 0]
        n = len(hunts)
        n_sessions = sum(1 for s in sessions.values() if s.get("hunts"))
        total_play = sum(s.get("play", 0) for s in sessions.values())
        return {
            "state": state, "hunt_elapsed": hunt_elapsed, "session_len": session_len,
            **self._mission_left(cur, state, now),
            "session_hunts": len(cur["hunts"]), "all_hunts": n,
            "last": hunts_last(cur, hunts), "avg": sum(durs) / n if n else None,
            "longest": max(durs) if durs else None, "shortest": min(durs) if durs else None,
            "level": self.level, "age": now - self.mtime if self.mtime else None,
            "pulse": now - self.changed_at < 1.6, "detailed": bool(self.cfg.get("detailed", True)),
            "total_time": sum(durs), "total_play": total_play, "sessions": n_sessions,
            "week_hours": week_hours,
            # Steam's all-time total is only saved when the game starts/stops, so add logged time since then.
            "steam_total": (steam[0] + logged_since(sessions, steam[1])) if steam else None,
            "per_session": n / n_sessions if n_sessions else None,
            "today_n": len(today_h), "today_time": sum(h["d"] for h in today_h),
            "week": week, "busiest": (busiest[0], len(busiest[1])) if busiest else None, "since": first_day,
            "quick_n": len(quick), "nodeath_n": len(nodeath),
            "avg_deaths": sum(h.get("deaths", 0) for h in hunts) / n if n else None,
            "avg_queue": sum(qs) / len(qs) if qs else None,
            "clash_n": sum(1 for h in hunts if h.get("mode") == "Bounty Clash"),
            "career": load_career() or {"empty": True}, "page": self.cfg.get("page", "tracker"),
            "summary_kills": self.summary_kills(), "wallet": self.wallet_view(),
            "hunter_kills": self.hunter_kills(),
            "hidden_families": self.cfg.get("hidden_families", []),
            "mmr": self.hist["mmr"][-1][1] if self.hist["mmr"] else None,
            "mmr_prev": self.hist["mmr"][-2][1] if len(self.hist["mmr"]) > 1 else None,
            "mmr_at": self.hist["mmr"][-1][0] if self.hist["mmr"] else None,
            "pl": self.players_view(), "players_panel": bool(self.cfg.get("players_panel", True)),
            **self._kd(cur, state, hunts),
        }


def day_hours(sessions: dict) -> dict:
    """Hours in game per calendar day; a session past midnight is split between the two days."""
    out: dict = {}
    for s in sessions.values():
        a, b = s.get("b"), s.get("b", 0) + s.get("play", 0)
        while a and a < b:
            day = dt.date.fromtimestamp(a)
            nxt = dt.datetime.combine(day + dt.timedelta(days=1), dt.time()).timestamp()
            out[day] = out.get(day, 0.0) + (min(b, nxt) - a) / 3600
            a = nxt
    return out


STAT_GRAPHS = {  # key: (title, "bar" or "line", how a value is written)
    "hunts": ("Hunts per day", "bar", str),
    "time_hunting": ("Time hunting per day", "bar", lambda x: fmt_hm(x)),
    "time_in_game": ("Time in game per day", "bar", lambda x: fmt_hm(x)),
    "steam": ("All-time hours (Steam)", "line", lambda x: fmt_hm(x)),
    "avg_hunt": ("Average hunt length per day", "line", lambda x: fmt_dur(x)),
    "longest": ("Longest / shortest hunt per day", "line", lambda x: fmt_dur(x)),
    "quick": ("Quick leaves (<5 min) per day", "bar", str),
    "nodeath": ("Hunts without a death screen (% per day)", "line", lambda x: f"{x:.0f}%"),
    "deaths": ("Death screens per hunt, per day", "line", lambda x: f"{x:.1f}"),
    "queue": ("Average queue wait per day", "line", lambda x: fmt_dur(x)),
    "clash": ("Bounty Clash hunts per day", "bar", str),
    "sessions": ("Sessions per day", "bar", str),
    "busiest": ("Hunts per day (busiest in gold)", "bar", str),
    "level": ("Hunter level", "line", lambda x: str(int(x))),
}


def stat_series(t, key: str):
    """(days, [(series name, colour, values per day)], highlighted day index) for one lifetime stat.
    A value of None means nothing to show that day (no hunts to average)."""
    sessions = t.hist["sessions"]
    hunts = [h for s_ in sessions.values() for h in s_.get("hunts", []) if h.get("t")]
    by_day: dict = {}
    for h in hunts:
        by_day.setdefault(dt.date.fromtimestamp(h["t"]), []).append(h)
    starts = [dt.date.fromtimestamp(s_["b"]) for s_ in sessions.values() if s_.get("b") and s_.get("hunts")]
    levels = t.hist.get("levels", [])
    first = min(list(by_day) + starts + [dt.date.fromtimestamp(a) for a, _ in levels[:1]] or [dt.date.today()])
    days = [first + dt.timedelta(days=i) for i in range((dt.date.today() - first).days + 1)]

    def per_day(f):
        return [f(by_day.get(d, [])) for d in days]

    def mean(xs):
        return sum(xs) / len(xs) if xs else None

    gold, red, cream = C["gold"], C["red"], C["cream"]
    hi = None
    if key in ("hunts", "busiest"):
        vals = per_day(len)
        hi = vals.index(max(vals)) if key == "busiest" and any(vals) else None
        out = [("hunts", gold, vals)]
    elif key == "time_hunting":
        out = [("time hunting", gold, per_day(lambda hs: sum(h["d"] for h in hs)))]
    elif key == "time_in_game":
        hrs = day_hours(sessions)
        out = [("time in game", gold, [hrs.get(d, 0.0) * 3600 for d in days])]
    elif key == "steam":
        steam = t._steam()
        if not steam:
            out = [("hours", gold, [None for _ in days])]
        else:
            total = steam[0] + logged_since(sessions, steam[1])
            ends = [dt.datetime.combine(d + dt.timedelta(days=1), dt.time()).timestamp() for d in days]
            out = [("all-time", gold, [total - logged_since(sessions, e) for e in ends])]
    elif key == "avg_hunt":
        out = [("average", gold, per_day(lambda hs: mean([h["d"] for h in hs])))]
    elif key == "longest":
        out = [("longest", gold, per_day(lambda hs: max((h["d"] for h in hs), default=None))),
               ("shortest", cream, per_day(lambda hs: min((h["d"] for h in hs), default=None)))]
    elif key == "quick":
        q = t.cfg["quick_leave_seconds"]
        out = [("quick leaves", gold, per_day(lambda hs: sum(1 for h in hs if h["d"] < q)))]
    elif key == "nodeath":
        out = [("no death screen", gold, per_day(
            lambda hs: 100 * sum(1 for h in hs if h.get("deaths", 0) == 0) / len(hs) if hs else None))]
    elif key == "deaths":
        out = [("per hunt", red, per_day(lambda hs: mean([h.get("deaths", 0) for h in hs])))]
    elif key == "queue":
        out = [("queue wait", gold, per_day(lambda hs: mean([h["q"] for h in hs if h.get("q") is not None])))]
    elif key == "clash":
        out = [("Bounty Clash", gold, per_day(lambda hs: sum(1 for h in hs if h.get("mode") == "Bounty Clash")))]
    elif key == "sessions":
        out = [("sessions", gold, [starts.count(d) for d in days])]
    elif key == "level":
        vals, lv, i = [], None, 0
        for d in days:  # the level at the end of each day, carried over days without a change
            while i < len(levels) and dt.date.fromtimestamp(levels[i][0]) <= d:
                lv, i = levels[i][1], i + 1
            vals.append(lv)
        if t.level and vals:
            vals[-1] = t.level
        out = [("level", gold, vals)]
    else:
        raise KeyError(key)
    return days, out, hi


def logged_since(sessions: dict, t0: float) -> float:
    return sum(max(0, s["b"] + s.get("play", 0) - max(s["b"], t0)) for s in sessions.values() if s.get("b"))


STEAM_APP = "594650"  # Hunt: Showdown 1896


def steam_playtime(log: Path):
    """Steam's own Hunt playtime from userdata/*/config/localconfig.vdf: (seconds, last-played epoch)."""
    roots = []
    try:
        roots.append(log.resolve().parents[4])  # ...\Steam\steamapps\common\Hunt Showdown 1896\USER\Game.log
    except (IndexError, OSError):
        pass
    roots += steam_roots()  # the game can sit in another library than Steam itself (which holds userdata)
    files, seen = [], set()
    for r in roots:
        for f in r.glob("userdata/*/config/localconfig.vdf"):
            if str(f).lower() not in seen:
                seen.add(str(f).lower())
                files.append(f)
    best = None
    for f in files:
        try:
            txt = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r'"%s"\s*\{([^{}]*)\}' % STEAM_APP, txt)
        if not m:
            continue
        pt = re.search(r'"Playtime"\s*"(\d+)"', m.group(1))
        lp = re.search(r'"LastPlayed"\s*"(\d+)"', m.group(1))
        if pt and (best is None or int(pt.group(1)) * 60 > best[0]):
            best = (int(pt.group(1)) * 60, int(lp.group(1)) if lp else time.time())
    return best


def hunts_last(cur: dict, hunts: list):
    if cur.get("hunts"):
        return cur["hunts"][-1]["d"]
    return hunts[-1]["d"] if hunts else None


# ------------------------------------------------------------------ the look (drawing instructions)
C = {"bg": "#0e0c0a", "panel": "#171310", "edge": "#4e1512", "edge_hi": "#b8312a", "inner": "#352b24",
     "brass": "#cfbb93", "cream": "#ece4d3", "dim": "#998d7a", "faint": "#2a221c",
     "red": "#cf3b32", "green": "#97c77f", "gold": "#d9a640", "bar": "#5c4a39", "blood": "#8e1b17"}
# Hunt's menus: an old-style serif for words, a tall narrow sans for numbers. Both ship with Windows.
TEXT_FONT, NUM_FONT = "Palatino Linotype", "Bahnschrift SemiCondensed"
STATES = {
    "menu": ("IN THE MENUS", C["dim"], "Ready when you are."),
    "search": ("SEARCHING FOR A MATCH", C["gold"], "Looking for hunters..."),
    "found": ("MATCH FOUND", C["brass"], "Loading in..."),
    "hunt": ("IN A HUNT", C["red"], "Hunt time"),
    "post": ("POST-MATCH", C["green"], "Back from the hunt."),
    "offline": ("HUNT ISN'T RUNNING", C["dim"], "Start the game and this wakes up."),
}


def pct(a, b) -> str:
    return f"{100 * a / b:.0f}%" if b else "-"


# MMR stars as the game draws them: white when earned, dark grey when not.
STAR_ON, STAR_OFF = "#d9a640", "#4a3c2e"  # gold when earned, dark when not


def mmr_star_ops(cx: float, cy: float, r: float, earned: bool) -> list:
    """A classic five-point MMR star."""
    import math
    pts = []
    for i in range(10):  # outer and inner points, alternating, starting straight up
        rad = r if i % 2 == 0 else r * 0.42
        ang = math.pi / 2 + i * math.pi / 5
        pts += [cx + rad * math.cos(ang), cy - rad * math.sin(ang) + r * 0.08]  # nudged down to look centred
    return [("poly", pts, STAR_ON if earned else STAR_OFF)]


def side_panel(v: dict):
    """What sits to the right of the main panel: Player Summary, or (when that's hidden) the detailed
    stats on their own on the Career page, so the window isn't one very tall column."""
    if v.get("players_panel"):
        return players_scene
    if v.get("page") == "career" and v.get("career") and v.get("detailed"):
        return detailed_scene
    return None


def columns(v: dict, wide: bool) -> list:
    """The panels, left to right, as functions (min_h, width) -> (W, H, ops). A wide window gets
    more, shorter columns, so the text can be bigger: status | career | Player Summary."""
    cols = []
    career = v.get("page") == "career" and v.get("career")
    if wide and career:
        cols.append(lambda mh, w: scene(v, mh, w, part="top"))
        cols.append(lambda mh, w: framed_scene(v, mh, w, "CAREER", "career"))
        if v.get("players_panel"):
            cols.append(lambda mh, w: players_scene(dict(v, detailed=False), mh, w))  # detailed is in column 1
        return cols
    cols.append(lambda mh, w: scene(v, mh, w))
    side = side_panel(v)
    if side is detailed_scene:
        cols.append(lambda mh, w: framed_scene(v, mh, w, "HUNT LOG", "detailed"))
    elif side:
        cols.append(lambda mh, w: side(v, mh, w))
    return cols


def full_scene(v: dict, panel_w: int = 360, wide: bool = False):
    """All panels side by side, all the same height. panel_w widens them to fill a wide window;
    360 is their normal width."""
    cols = columns(v, wide)
    drawn = [c(0, panel_w) for c in cols]
    H = max(d[1] for d in drawn)
    drawn = [c(H, panel_w) if d[1] < H else d for c, d in zip(cols, drawn)]
    ops, x = [], 0
    for w, _h, o in drawn:
        ops += shift_ops(o, x)
        x += w
    return x, H, ops


def detailed_scene(v: dict, min_h: int = 0, W: int = 360):
    return framed_scene(v, min_h, W, "HUNT LOG", "detailed")


def framed_scene(v: dict, min_h: int, W: int, title: str, block: str):
    """A panel of its own for one block of the tracker: "detailed" (Today / Last 7 days / Lifetime)
    or "career" (your Career stats and weapon kills)."""
    PAD = 16
    ops: list = []
    ops.append(("poly", [PAD + 5, 20, PAD + 10, 25, PAD + 5, 30, PAD, 25], C["brass"]))
    ops.append(("text", PAD + 18, 14, title, 19, C["brass"], "start", True, False))
    ops.append(("line", PAD, 44, W // 2 - 10, 44, C["brass"], 1))
    ops.append(("line", W // 2 + 10, 44, W - PAD, 44, C["brass"], 1))
    ops.append(("poly", [W // 2, 40, W // 2 + 4, 44, W // 2, 48, W // 2 - 4, 44], C["brass"]))
    _, y, more = scene(v, 0, W, block_at=62, block=block)
    ops.extend(more)
    H = max(y + 10, min_h)
    edge = C["edge_hi"] if v["pulse"] else C["edge"]
    ops.append(("rect", 1, 1, W - 1, H - 1, "none", edge, 2))
    ops.append(("rect", 5, 5, W - 5, H - 5, "none", C["inner"], 1))
    for cx, cy, dx, dy in ((9, 9, 1, 1), (W - 9, 9, -1, 1), (9, H - 9, 1, -1), (W - 9, H - 9, -1, -1)):
        ops.append(("line", cx, cy, cx + dx * 12, cy, C["brass"], 2))
        ops.append(("line", cx, cy, cx, cy + dy * 12, C["brass"], 2))
    return W, H, ops


def shift_ops(ops: list, dx: int) -> list:
    out = []
    for op in ops:
        k = op[0]
        if k in ("rect", "line", "hot"):
            out.append((k, op[1] + dx, op[2], op[3] + dx, *op[4:]))
        elif k == "poly":
            out.append((k, [c + dx if i % 2 == 0 else c for i, c in enumerate(op[1])], op[2]))
        else:  # circle, text: x is the first number
            out.append((k, op[1] + dx, *op[2:]))
    return out


def players_scene(v: dict, min_h: int = 0, W: int = 360):
    """PLAYER SUMMARY: who's on the loading cards this match, who killed you, who you keep meeting."""
    PAD = 16
    pl = v["pl"]
    ops: list[tuple] = []

    def rect(x0, y0, x1, y1, fill="none", stroke=None, sw=1):
        ops.append(("rect", x0, y0, x1, y1, fill, stroke, sw))

    def line(x0, y0, x1, y1, col, sw=1):
        ops.append(("line", x0, y0, x1, y1, col, sw))

    def diamond(cx, cy, r, col):
        ops.append(("poly", [cx, cy - r, cx + r, cy, cx, cy + r, cx - r, cy], col))

    def text(x, y, t, size, col, anchor="start", bold=False, mono=False):
        ops.append(("text", x, y, t, size, col, anchor, bold, mono))

    def section(y, title):
        diamond(PAD + 3, y + 8, 3, C["blood"])
        text(PAD + 11, y, title, 11, C["brass"], bold=True)
        line(PAD + 11 + int(7.6 * len(title)) + 8, y + 8, W - PAD, y + 8, C["blood"])
        return y + 21

    def clip(s, n=22):
        return s if len(s) <= n else s[:n - 1] + "…"

    def ago(ts):
        return fmt_ago(time.time() - ts) if ts else "-"

    diamond(PAD + 5, 25, 5, C["brass"])
    text(PAD + 18, 14, "PLAYER SUMMARY", 19, C["brass"], bold=True)
    line(PAD, 44, W // 2 - 10, 44, C["brass"])
    line(W // 2 + 10, 44, W - PAD, 44, C["brass"])
    diamond(W // 2, 44, 4, C["brass"])
    y = 60
    text(PAD, y, f"{pl['n_players']} players logged  ·  {pl['n_matches']} matches read", 12, C["dim"])
    y += 26
    if pl.get("mmr_warning"):
        text(PAD, y, "⚠ Warning: missing other teams' MMRs", 12, C["red"], bold=True)
        text(PAD, y + 17, "Open Team Details on the summary and scroll down.", 10, C["dim"])
        y += 40

    y = section(y, "YOUR SQUAD" if pl.get("squad_mode") else "THIS MATCH")
    if pl["this"]:
        pass
    elif pl["match_at"] is None:
        text(PAD, y, "Find a match: the loading cards fill this in.", 12, C["dim"])
        y += 26
    else:
        text(PAD, y, f"Started {ago(pl['match_at'])}, no player names read yet.", 12, C["dim"])
        y += 26
    for p in pl["this"]:
        rows_n = (min(3, len(p["stats"])) or (1 if p.get("hidden") else 0)) + (1 if p.get("mmr") is not None else 0)
        rect(PAD - 4, y, W - PAD + 4, y + 22 + 17 * rows_n, C["panel"], C["inner"])
        text(PAD + 4, y + 4, clip(p["name"], 19 if p["seen"] > 1 else 30), 14, C["cream"], bold=True)
        if p["seen"] > 1:
            text(W - PAD - 4, y + 6, f"(encountered {p['seen']} times)", 10, C["gold"], "end", bold=True)
        yy = y + 22
        if p.get("mmr") is not None:  # known from a match you played with or against them before
            text(PAD + 12, yy, "MMR (last time)", 11, C["dim"])
            for i in range(6):
                cx = W - PAD - 4 - 7 - (5 - i) * 16
                ops.extend(mmr_star_ops(cx, yy + 7, 7.5, i < p["mmr"]))
            yy += 17
        if p.get("hidden") and not p["stats"]:
            text(PAD + 12, yy, "stats hidden", 11, C["dim"])
            yy += 17
        for lab, val in list(p["stats"].items())[:3]:
            text(PAD + 12, yy, clip(lab, 30), 11, C["dim"])
            text(W - PAD - 4, yy - 1, f"{val:,}" if isinstance(val, int) else str(val), 12, C["cream"], "end",
                 bold=True, mono=True)
            yy += 17
        y = yy + 8
    y += 8

    y = section(y, "KILLED BY (THIS MATCH)")
    if not pl["killers"]:
        text(PAD, y, "Nobody yet this match.", 12, C["dim"])
        y += 24
    for n, c, weapon in pl["killers"][:8]:
        text(PAD, y, clip(n, 24), 13, C["cream"])
        if c > 1:  # they've killed you before: every time, all matches
            text(W - PAD - 4, y, f"×{c}", 13, C["red"], "end", bold=True, mono=True)
        y += 18
        if weapon:
            text(PAD + 12, y, f"with {clip(weapon, 32)}", 10, C["dim"])
            y += 16
        y += 4
    y += 8

    if v.get("page") == "career" and v.get("career") and v.get("detailed"):
        _, y, more = scene(v, 0, W, block_at=y + 6)  # the tracker page's detailed stats, in the free space
        ops.extend(more)
    if pl.get("note"):
        text(PAD, y, pl["note"], 10, C["red"])
        y += 18
    lr = pl["last_read"]
    if lr:
        what = lr.get("name") or lr.get("killer") or "no name found"
        text(PAD, y, f"last read: {lr['kind']} · {clip(what, 20)} · {ago(lr['t'])}", 10, C["dim"])
        y += 20
    H = max(y + 10, min_h)

    edge = C["edge_hi"] if v["pulse"] else C["edge"]
    rect(1, 1, W - 1, H - 1, "none", edge, 2)
    rect(5, 5, W - 5, H - 5, "none", C["inner"], 1)
    for cx, cy, dx, dy in ((9, 9, 1, 1), (W - 9, 9, -1, 1), (9, H - 9, 1, -1), (W - 9, H - 9, -1, -1)):
        line(cx, cy, cx + dx * 12, cy, C["brass"], 2)
        line(cx, cy, cx, cy + dy * 12, C["brass"], 2)
    return W, H, ops


def scene(v: dict, min_h: int = 0, W: int = 360, block_at=None, block: str = "detailed", part=None):
    """Describe the panel as drawing instructions: (width, height, ops). block_at: only draw the
    detailed stats (Today / Last 7 days / Lifetime) starting at that height, for another panel."""
    PAD = 16
    ops: list[tuple] = []

    def rect(x0, y0, x1, y1, fill="none", stroke=None, sw=1):
        ops.append(("rect", x0, y0, x1, y1, fill, stroke, sw))

    def line(x0, y0, x1, y1, col, sw=1):
        ops.append(("line", x0, y0, x1, y1, col, sw))

    def poly(pts, col):
        ops.append(("poly", pts, col))

    def diamond(cx, cy, r, col):
        poly([cx, cy - r, cx + r, cy, cx, cy + r, cx - r, cy], col)

    def text(x, y, t, size, col, anchor="start", bold=False, mono=False):
        ops.append(("text", x, y, t, size, col, anchor, bold, mono))

    def section(y, title):
        diamond(PAD + 3, y + 8, 3, C["blood"])
        text(PAD + 11, y, title, 11, C["brass"], bold=True)
        line(PAD + 11 + int(7.6 * len(title)) + 8, y + 8, W - PAD, y + 8, C["blood"])
        return y + 21

    # header
    diamond(PAD + 5, 25, 5, C["brass"])
    text(PAD + 18, 14, "HUNT TRACKER", 19, C["brass"], bold=True)
    text(W - PAD, 19, fmt_ago(v["age"]) if v["age"] is not None else "", 12, C["dim"], "end")
    line(PAD, 44, W // 2 - 10, 44, C["brass"])
    line(W // 2 + 10, 44, W - PAD, 44, C["brass"])
    diamond(W // 2, 44, 4, C["brass"])

    def cells(y, items, h=56):
        """Boxes of LABEL / value; an item can carry a third part, a green "+N" shown under it."""
        if any(len(it) > 2 and it[2] for it in items):
            h += 12
        rect(PAD - 4, y, W - PAD + 4, y + h, C["panel"], C["inner"])
        cw = (W - 2 * PAD) / len(items)
        for i, it in enumerate(items):
            lab, val = it[0], it[1]
            cx = int(PAD + cw * i + cw / 2)
            text(cx, y + 9, lab, 10, C["dim"], "middle", bold=True)
            text(cx, y + 26, val, 17, C["cream"], "middle", bold=True, mono=True)
            if len(it) > 2 and it[2]:
                text(cx, y + 46, it[2], 10, C["green"], "middle", bold=True)
            if i:
                line(int(PAD + cw * i), y + 10, int(PAD + cw * i), y + h - 10, C["inner"])
        return y + h

    def rows(y, items):
        for a_, b_, *key in items:
            if key:  # clickable: opens this stat's graph
                ops.append(("hot", PAD - 4, y - 2, W - PAD + 4, y + 19, key[0]))
            text(PAD, y, a_, 13, C["dim"])
            text(W - PAD, y - 1, b_, 14, C["cream"], "end", bold=True, mono=True)
            y += 21
        return y

    def num(x):
        return f"{x:,}" if isinstance(x, int) else f"{x:.2f}" if isinstance(x, float) else str(x)

    def rows2(y, items, icons=False, deltas=None):
        """Two columns of label/value pairs, filled top-to-bottom then left-to-right. icons: draw
        each stat's icon before its name. deltas: {label: +N since your stats were last checked}."""
        half = (len(items) + 1) // 2
        cw = (W - 2 * PAD) / 2
        for i, (a_, b_) in enumerate(items):
            col_x = PAD + cw * (i // half)
            ry = y + 19 * (i % half)
            tx = int(col_x)
            if icons and icon_png(a_, 8):
                ops.append(("image", tx, ry, a_, 14))
                tx += 18
            text(tx, ry, a_, 12, C["dim"])
            text(int(col_x + cw - 12), ry - 1, num(b_), 13, C["cream"], "end", bold=True, mono=True)
            d = (deltas or {}).get(a_)
            if d:
                text(int(col_x + cw - 12 - 8 * len(num(b_)) - 6), ry, f"+{num(d)}", 10, C["green"], "end", bold=True)
        if half > 0:
            line(int(PAD + cw - 4), y, int(PAD + cw - 4), y + 19 * half - 4, C["inner"])
        return y + 19 * half

    def career_panel(y):
        c = v["career"]
        hd = c.get("headline", {})
        who = c.get("name") or ("not read yet: open Statistics in Hunt's menu" if c.get("empty") else "")
        if c.get("prestige") is not None:
            who += f"  ·  prestige {c['prestige']}, rank {c.get('rank', '-')}"
        y = section(y, "CAREER (in-game profile)")
        text(PAD, y, who, 12, C["cream"], bold=True)
        y += 20
        dl = c.get("deltas", {})  # what went up since you last checked your stats; cleared each match

        def box(label, key, src=hd, unit=""):
            val = src.get(key, 0)  # not read yet: 0
            d = dl.get(key)
            return label, num(val) + unit, f"+{num(d)}{unit}" if d else ""

        y = cells(y, [box("KILLS", "Players Killed"), box("DEATHS", "Killed by Player"),
                      box("ASSISTS", "Kill Assists")], 52) + 6
        y = cells(y, [box("K/D", "K/D"), box("KDA", "KDA"), box("WIPES", "Teams Wiped"),
                      box("HEADSHOTS", "Headshots")]) + 6
        y = cells(y, [box("BOUNTY", "Total Bounty"), box("MONSTERS", "Monsters Killed"),
                      box("BOSSES", "Boss Targets Killed")], 52) + 6
        other, prof = dict(c.get("other", {})), c.get("profile_stats", {})
        y = cells(y, [box("FURTHEST KILL", "Furthest Kill Distance", prof, "m"),
                      box("MAXED-OUT HUNTERS", "Maxed-Out Hunters", other)], 52) + 6
        other.pop("Maxed-Out Hunters", None)  # it has its own box now
        other.update({k: 0 for k in CAREER_OTHER if k not in other})
        y += 4
        y = rows2(y, list(other.items()), deltas=dl) + 8
        types = dict(c.get("weapon_types", {}))
        types.update({k: 0 for k in CAREER_TYPES if k not in types})
        y = section(y, "KILLS BY WEAPON TYPE")
        y = rows2(y, list(types.items()), icons=True, deltas=dl) + 8
        fams = c.get("weapon_families", {})
        hidden = set(v.get("hidden_families") or [])
        shown = [(k, n) for k, n in fams.items() if k not in hidden]
        tv = c.get("top_variant")
        y = section(y, FAMILY_HEAD)
        if tv and (family_of(tv["name"]) not in hidden):
            rect(PAD - 4, y - 3, W - PAD + 4, y + 19, C["panel"], C["inner"])
            tx = PAD + 2
            if icon_png(tv["name"], 8):
                ops.append(("image", tx, y, tv["name"], 14))
                tx += 18
            text(tx, y, f"Most kills: {tv['name']}", 12, C["gold"], bold=True)
            text(W - PAD - 2, y - 1, num(tv["n"]), 13, C["gold"], "end", bold=True, mono=True)
            y += 26
        if shown:
            y = rows2(y, shown, icons=True, deltas=dl) + 6
        elif not fams:
            text(PAD, y, "not read yet: open your weapon kills list in Hunt's menu", 11, C["dim"])
            y += 24
        elif not (tv and family_of(tv["name"]) not in hidden):
            text(PAD, y, "all hidden: click the heading to pick which to show", 11, C["dim"])
            y += 24
        if c.get("empty"):
            text(PAD, y, "fills in when you open your Statistics pages in Hunt", 10, C["dim"])
        else:
            text(PAD, y, f"from your profile, {c.get('updated', '?')}  ·  edit hunttracker_career.json", 10, C["dim"])
        return y + 18

    def star(cx, cy, r, earned):
        ops.extend(mmr_star_ops(cx, cy, r, earned))

    def mmr_row(y):
        stars = v.get("mmr")
        rect(PAD - 4, y, W - PAD + 4, y + 38, C["panel"], C["inner"])
        text(PAD + 6, y + 12, "MMR", 13, C["brass"], bold=True)
        for i in range(6):
            star(PAD + 72 + i * 25, y + 19, 12, bool(stars and i < stars))
        if stars:
            prev = v.get("mmr_prev")
            if prev and prev != stars:
                note, ncol = (f"▲ from {prev}", C["green"]) if stars > prev else (f"▼ from {prev}", C["red"])
            else:
                note, ncol = f"{stars} / 6", C["cream"]
            text(W - PAD - 4, y + 11, note, 14, ncol, "end", bold=True, mono=True)
        else:
            text(W - PAD - 4, y + 12, "not read yet", 11, C["dim"], "end")
        y += 44
        total, src, extra = known_kills(v.get("career"), v.get("summary_kills", 0))
        if total is not None:
            h = 36 + (16 if extra else 0)
            rect(PAD - 4, y, W - PAD + 4, y + h, C["panel"], C["inner"])
            text(PAD + 6, y + 10, "TOTAL KNOWN KILLS", 13, C["brass"], bold=True)
            text(W - PAD - 4, y + 6, f"{total:,}", 20, C["cream"], "end", bold=True, mono=True)
            if extra:
                text(PAD + 6, y + 32, f"+ {extra} from match summaries since your stats were read", 10, C["green"])
            y += h + 6
        if v.get("hunter_kills"):
            hunter, n = v["hunter_kills"]
            rect(PAD - 4, y, W - PAD + 4, y + 32, C["panel"], C["inner"])
            text(PAD + 6, y + 9, f"{hunter.upper()} KILLS", 12, C["brass"], bold=True)
            text(W - PAD - 4, y + 6, f"{n:,}", 17, C["cream"], "end", bold=True, mono=True)
            y += 38
        if v.get("wallet"):
            dollars, bonds, plus_d, plus_b = v["wallet"]
            pending = plus_d or plus_b
            hgt = 50 + (14 if pending else 0)
            rect(PAD - 4, y, W - PAD + 4, y + hgt, C["panel"], C["inner"])
            half = (W - 2 * PAD) // 2
            line(PAD + half, y + 8, PAD + half, y + hgt - 8, C["inner"])
            for i, (lab, val, plus) in enumerate((("HUNT DOLLARS", dollars, plus_d), ("BLOOD BONDS", bonds, plus_b))):
                cx = PAD + half * i + half // 2
                text(cx, y + 8, lab, 10, C["dim"], "middle", bold=True)
                # The total stays at what's banked; this match's pickup only counts if you extract,
                # and the menu shows the real total after the match.
                text(cx, y + 24, f"{val:,}", 17, C["cream"], "middle", bold=True, mono=True)
                if pending:
                    text(cx, y + 46, f"+{plus:,} if you extract", 10, C["green"], "middle")
            y += hgt + 6
        return y + 6

    def detailed_block(y):
        """Today, the last 7 days and lifetime totals from your logs."""
        y = section(y, "TODAY")
        y = cells(y, [("HUNTS", str(v["today_n"])), ("TIME HUNTING", fmt_hm(v["today_time"]) if v["today_n"] else "-"),
                      ("THIS SESSION", fmt_hm(v["session_len"]) if v["session_len"] is not None else "-")], 52) + 14

        def week_chart(y, items, label):
            mx = max([n for _, n in items] + [1])
            slot = (W - 2 * PAD) / 7
            base_y = y + 52
            for i, (day, n) in enumerate(items):
                cx = int(PAD + slot * i + slot / 2)
                hgt = max(2, int(40 * n / mx)) if n else 2
                rect(cx - 12, base_y - hgt, cx + 12, base_y, C["gold"] if i == 6 else C["bar"])
                if n:
                    text(cx, base_y - hgt - 14, label(n), 11, C["cream"], "middle", bold=True, mono=True)
                text(cx, base_y + 3, "MTWTFSS"[day.weekday()], 10, C["dim"], "middle", bold=True)
            return base_y + 22

        y = section(y, f"LAST 7 DAYS · {sum(n for _, n in v['week'])} HUNTS · {sum(h for _, h in v['week_hours']):.1f}H")
        y = week_chart(y, v["week"], str)

        y = section(y, "LIFETIME (from your logs)")
        since = v["since"].strftime("%b %d") if v["since"] else "-"
        bus = f"{v['busiest'][0].strftime('%b %d')} · {v['busiest'][1]}" if v["busiest"] else "-"
        y = rows(y + 2, [
            ("Hunts tracked", f"{v['all_hunts']}  since {since}", "hunts"),
            ("Time hunting", fmt_hm(v["total_time"]), "time_hunting"),
            ("Time in game", fmt_hm(v["total_play"]), "time_in_game"),
            ("All-time (Steam)", fmt_hm(v["steam_total"]) if v.get("steam_total") else "-", "steam"),
            ("Average hunt", fmt_dur(v["avg"]) if v["avg"] else "-", "avg_hunt"),
            ("Longest / shortest", f"{fmt_dur(v['longest'])} / {fmt_dur(v['shortest'])}" if v["longest"] else "-",
             "longest"),
            ("Quick leaves (<5 min)", f"{v['quick_n']}  ({pct(v['quick_n'], v['all_hunts'])})", "quick"),
            ("Hunts w/o death screen*", f"{v['nodeath_n']}  ({pct(v['nodeath_n'], v['all_hunts'])})", "nodeath"),
            ("Death screens per hunt", f"{v['avg_deaths']:.1f}" if v["avg_deaths"] is not None else "-", "deaths"),
            ("Average queue wait", fmt_dur(v["avg_queue"]) if v["avg_queue"] is not None else "-", "queue"),
            ("Bounty Clash hunts", str(v["clash_n"]), "clash"),
            ("Sessions", f"{v['sessions']}  ({v['per_session']:.1f} hunts each)" if v["per_session"] else "-",
             "sessions"),
            ("Busiest day", bus, "busiest"),
            ("Hunter level", str(v["level"] or "-"), "level"),
        ])
        text(PAD, y + 2, "* no death screen shown = probably survived (estimate)", 10, C["dim"])
        text(PAD, y + 17, "click a stat to see its graph", 10, C["dim"])
        y += 44
        return y

    if block_at is not None:  # just one block, for another panel
        ops.clear()
        return W, (career_panel if block == "career" else detailed_block)(block_at), ops

    # state block
    label, col, sub = STATES.get(v["state"], STATES["menu"])
    y = 62
    ops.append(("circle", PAD + 6, y + 9, 6, col))
    text(PAD + 20, y, label, 16, col, bold=True)
    if v["state"] == "hunt" and v.get("mission_left") is not None:
        left = v["mission_left"]
        colr = C["red"] if left < 5 * 60 else C["cream"]  # last five minutes
        text(W // 2, y + 30, ("" if v["mission_exact"] else "~") + fmt_dur(left), 46, colr, "middle", bold=True, mono=True)
        text(W // 2, y + 84, "mission ends in" + ("" if v["mission_exact"] else "  (hold Tab on the map to sync)"),
             12, C["dim"], "middle")
        y += 108
    else:
        text(PAD + 20, y + 26, sub, 13, C["dim"])
        y += 56
    y = mmr_row(y)

    if part != "top" and v.get("page") == "career" and v.get("career"):  # "top": career has its own column
        y = career_panel(y)
        text(PAD, y, "click CAREER at the top to switch back", 10, C["dim"])
        y += 26
    elif not v["detailed"]:
        line(PAD, y, W - PAD, y, C["inner"])
        y = rows(y + 10, [("Last hunt", fmt_dur(v["last"]) if v["last"] else "-"),
                          ("Longest", fmt_dur(v["longest"]) if v["longest"] else "-"),
                          ("This session", fmt_dur(v["session_len"]) if v["session_len"] is not None else "-")]) + 8
        y = cells(y, [("SESSION", str(v["session_hunts"])), ("ALL-TIME", str(v["all_hunts"])),
                      ("AVERAGE", fmt_dur(v["avg"]) if v["avg"] else "-"), ("LEVEL", str(v["level"] or "-"))]) + 8
    else:
        y = detailed_block(y)
    H = max(y, min_h)

    # frame (last, so it sits on top)
    edge = C["edge_hi"] if v["pulse"] else C["edge"]
    rect(1, 1, W - 1, H - 1, "none", edge, 2)
    rect(5, 5, W - 5, H - 5, "none", C["inner"], 1)
    for cx, cy, dx, dy in ((9, 9, 1, 1), (W - 9, 9, -1, 1), (9, H - 9, 1, -1), (W - 9, H - 9, -1, -1)):
        line(cx, cy, cx + dx * 12, cy, C["brass"], 2)
        line(cx, cy, cx, cy + dy * 12, C["brass"], 2)
    return W, H, ops


# Stat icons cut from the game's own screens (installer/make_weapon_icons.py). Shipped inside the .exe.
ICON_DIR = (Path(getattr(sys, "_MEIPASS", HERE)) if FROZEN else HERE) / "icons"
_icon_cache: dict = {}


def _icon_rgba(name: str):
    """(w, h, RGBA rows) of icons/<name>.png, or None if there's no icon for that stat."""
    import struct
    import zlib
    key = ("raw", name)
    if key not in _icon_cache:
        f = ICON_DIR / (name.replace("/", "-") + ".png")
        if not f.exists() and family_of(name):
            f = ICON_DIR / (FAMILY_CLASS[family_of(name)] + ".png")
        elif not f.exists() and name == "Carbine":
            f = ICON_DIR / "Rifle.png"
        val = None
        if f.exists():
            d = f.read_bytes()
            w, h = struct.unpack(">II", d[16:24])
            pos, idat = 8, b""
            while pos < len(d):
                n = struct.unpack(">I", d[pos:pos + 4])[0]
                if d[pos + 4:pos + 8] == b"IDAT":
                    idat += d[pos + 8:pos + 8 + n]
                pos += 12 + n
            raw, s = zlib.decompress(idat), w * 4
            val = (w, h, [raw[y * (s + 1) + 1:(y + 1) * (s + 1)] for y in range(h)])  # written unfiltered
        _icon_cache[key] = val
    return _icon_cache[key]


def icon_png(name: str, box: int):
    """PNG bytes of a stat icon shrunk to fit a box x box square (smoothly, keeping its shape)."""
    import struct
    import zlib
    key = (name, box)
    if key in _icon_cache:
        return _icon_cache[key]
    src = _icon_rgba(name)
    if not src or box < 2:
        _icon_cache[key] = None
        return None
    w, h, rows = src
    f = box / max(w, h)
    tw, th = max(1, round(w * f)), max(1, round(h * f))
    out = bytearray()
    for ty in range(th):
        y0, y1 = int(ty / f), max(int(ty / f) + 1, int((ty + 1) / f))
        out += b"\x00"
        for tx in range(tw):
            x0, x1 = int(tx / f), max(int(tx / f) + 1, int((tx + 1) / f))
            acc = [0, 0, 0, 0]
            n = 0
            for y in range(y0, min(y1, h)):
                row = rows[y]
                for x in range(x0, min(x1, w)):
                    r, g, b, a = row[x * 4:x * 4 + 4]
                    acc[0] += r * a
                    acc[1] += g * a
                    acc[2] += b * a
                    acc[3] += a
                    n += 1
            a = acc[3]
            out += bytes((acc[0] // a, acc[1] // a, acc[2] // a, a // max(1, n))) if a else b"\x00\x00\x00\x00"

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", tw, th, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(out), 6)) + chunk(b"IEND", b""))
    _icon_cache[key] = data
    return data


# The picture behind the panels (made with ComfyUI). Shipped inside the .exe.
BACKDROP = (Path(getattr(sys, "_MEIPASS", HERE)) if FROZEN else HERE) / "backdrop.png"
_backdrop: dict = {}


def backdrop_png(cw: int, ch: int, opacity: float, focus=(0.5, 0.55), at=(0.5, 0.5), panels=()):
    """PNG bytes of the backdrop at exactly cw x ch: scaled to cover the window (never stretched),
    cropped so `focus` (the hunter) lands at `at` in the window, and faded into the background colour
    so text stays readable. `panels` (pixel boxes) are darkened further, with slightly ragged edges,
    so the picture shows faintly through them like Hunt's own menus."""
    import struct
    import zlib
    key = (cw, ch, round(opacity, 2), at, panels)
    if key in _backdrop:
        return _backdrop[key]
    if "src" not in _backdrop:
        _backdrop["src"] = read_png(BACKDROP) if BACKDROP.exists() else None
    src = _backdrop["src"]
    if not src or cw < 2 or ch < 2:
        return None
    w, h, bgra = src
    if ("blend", opacity) not in _backdrop:  # each source row as RGB, already faded toward the background
        bg = (20, 16, 13)
        rows = []
        for y in range(h):
            line = bgra[y * w * 4:(y + 1) * w * 4]
            rgb = bytearray(w * 3)
            for i, c in enumerate((2, 1, 0)):
                ch_ = line[c::4]
                rgb[i::3] = bytes(int(bg[i] + (v - bg[i]) * opacity) for v in ch_)
            rows.append(bytes(rgb))
        _backdrop[("blend", opacity)] = rows
    rows = _backdrop[("blend", opacity)]
    dark_op = opacity * 0.32  # inside a panel: the picture is only a faint texture
    if ("blend", dark_op) not in _backdrop:
        pc = (21, 17, 14)
        drows = []
        for y in range(h):
            line = bgra[y * w * 4:(y + 1) * w * 4]
            rgb = bytearray(w * 3)
            for i, c in enumerate((2, 1, 0)):
                rgb[i::3] = bytes(int(pc[i] + (v - pc[i]) * dark_op) for v in line[c::4])
            drows.append(bytes(rgb))
        _backdrop[("blend", dark_op)] = drows
    drows = _backdrop[("blend", dark_op)]

    def rag(a: int, b: int) -> int:  # 0-2 px of wear, the same every time for the same spot
        return ((a * 73856093) ^ (b * 19349663) ^ 0x5bd1e995) % 3
    scale = max(cw / w, ch / h)
    ox = min(max(focus[0] * w * scale - cw * at[0], 0), w * scale - cw)
    oy = min(max(focus[1] * h * scale - ch * at[1], 0), h * scale - ch)
    xs = [min(w - 1, int((x + ox) / scale)) * 3 for x in range(cw)]
    out = []
    for y in range(ch):
        sy = min(h - 1, int((y + oy) / scale))
        row = b"".join(rows[sy][i:i + 3] for i in xs)
        hits = [b for b in panels if b[1] <= y < b[3]]
        if hits:
            drow = b"".join(drows[sy][i:i + 3] for i in xs)
            row = bytearray(row)
            for x0, y0, x1, y1 in hits:
                a, b = x0 + rag(y, x0), x1 - rag(y, x1)
                if y - y0 < 2 or y1 - y < 3:  # torn top / bottom edge: a few chunks missing
                    for cx in range(a, b, 4):
                        if rag(cx // 4, y0 + y1) or y - y0 == 1 or y1 - y == 2:
                            row[cx * 3:min(cx + 4, b) * 3] = drow[cx * 3:min(cx + 4, b) * 3]
                else:
                    row[a * 3:b * 3] = drow[a * 3:b * 3]
            row = bytes(row)
        out.append(b"\x00" + row)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", cw, ch, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(out), 1)) + chunk(b"IEND", b""))
    for k in [k for k in _backdrop if isinstance(k, tuple) and len(k) == 4]:
        del _backdrop[k]  # only the current size is worth keeping
    _backdrop[key] = data
    return data


def build_svg(v: dict) -> str:
    W, H, ops = full_scene(v)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">',
           f'<rect width="{W}" height="{H}" fill="{C["bg"]}"/>']
    for op in ops:
        k = op[0]
        if k == "rect":
            _, x0, y0, x1, y1, fill, stroke, sw = op
            out.append(f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" fill="{fill}"'
                       + (f' stroke="{stroke}" stroke-width="{sw}"' if stroke else "") + "/>")
        elif k == "line":
            _, x0, y0, x1, y1, col, sw = op
            out.append(f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y1}" stroke="{col}" stroke-width="{sw}"/>')
        elif k == "poly":
            _, pts, col = op
            pp = " ".join(f"{pts[i]},{pts[i + 1]}" for i in range(0, len(pts), 2))
            out.append(f'<polygon points="{pp}" fill="{col}"/>')
        elif k == "circle":
            _, cx, cy, r, col = op
            out.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{col}"/>')
        elif k == "image":
            _, x, y, name, box = op
            data = icon_png(name, int(box * 2))  # sharper on big screens
            if data:
                import base64
                out.append(f'<image x="{x}" y="{y}" width="{box}" height="{box}" '
                           f'href="data:image/png;base64,{base64.b64encode(data).decode()}"/>')
        elif k == "text":
            _, x, y, t, size, col, anchor, bold, mono = op
            fam = (f"'{NUM_FONT}',Bahnschrift,'Arial Narrow',sans-serif" if mono
                   else f"'{TEXT_FONT}',Palatino,Georgia,serif")
            out.append(f'<text x="{x}" y="{y}" fill="{col}" font-family="{fam}" font-size="{size}" '
                       f'font-weight="{"bold" if bold else "normal"}" text-anchor="{anchor}" dominant-baseline="hanging">{escape(t)}</text>')
    out.append("</svg>")
    return "\n".join(out)


def draw_ops(canvas, W, H, ops, k=1.0) -> None:
    """Paint the scene on a tkinter Canvas, scaled by k."""
    canvas.delete("all")
    canvas.icons = []  # Tk drops images that nothing keeps a reference to
    canvas.hot = [(o[1] * k, o[2] * k, o[3] * k, o[4] * k, o[5]) for o in ops if o[0] == "hot"]
    anchors = {"start": "nw", "middle": "n", "end": "ne"}
    for op in ops:
        kind = op[0]
        if kind == "image":
            _, x, y, name, box = op
            size = max(4, round(box * k))
            data = icon_png(name, size)
            if data:
                import base64
                import tkinter as tk
                img = tk.PhotoImage(data=base64.b64encode(data))
                canvas.icons.append(img)
                # centred in its box, since icons aren't all square
                canvas.create_image((x + box / 2) * k, (y + box / 2) * k, image=img, anchor="center")
            continue
        if kind == "rect":
            _, x0, y0, x1, y1, fill, stroke, sw = op
            canvas.create_rectangle(x0 * k, y0 * k, x1 * k, y1 * k, fill="" if fill == "none" else fill,
                                    outline=stroke or "", width=max(1, round(sw * k)) if stroke else 0)
        elif kind == "line":
            _, x0, y0, x1, y1, col, sw = op
            canvas.create_line(x0 * k, y0 * k, x1 * k, y1 * k, fill=col, width=max(1, round(sw * k)))
        elif kind == "poly":
            _, pts, col = op
            canvas.create_polygon(*[c * k for c in pts], fill=col, outline="")
        elif kind == "circle":
            _, cx, cy, r, col = op
            canvas.create_oval((cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k, fill=col, outline="")
        elif kind == "text":
            _, x, y, t, size, col, anchor, bold, mono = op
            font = (NUM_FONT if mono else TEXT_FONT, -max(6, int(round(size * (1.12 if mono else 1.0) * k))),
                    "bold" if bold else "normal")
            canvas.create_text(x * k, y * k, text=t, font=font, fill=col, anchor=anchors[anchor],
                               tags=("famhead",) if t == FAMILY_HEAD else ())


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hunt Tracker</title><style>
html,body{margin:0;min-height:100%;background:#0b0907;color:#8a7b66;font-family:Georgia,serif}
body{display:grid;place-items:center;padding:12px 0} #box{width:min(94vw,560px)} #box svg{width:100%;height:auto;display:block}
#msg{position:fixed;bottom:10px;left:0;right:0;text-align:center;font-size:12px}
</style></head><body><div id="box"></div><div id="msg">connecting...</div><script>
const box=document.getElementById('box'),msg=document.getElementById('msg');let last='';
async function tick(){try{const r=await fetch('/scene.svg',{cache:'no-store'});const t=await r.text();
if(t!==last){box.innerHTML=t;last=t}msg.textContent=''}catch(e){msg.textContent='Lost connection to the tracker program. Is its window still open?'}}
tick();setInterval(tick,1000);
</script></body></html>"""


# ------------------------------------------------------------------ runners
def lan_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "your-pc-ip"
    finally:
        s.close()


def on_a_monitor(x: int, y: int) -> bool:
    """Whether a window corner at (x, y) would land on a connected monitor."""
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes
    pt = wintypes.POINT(int(x) + 40, int(y) + 20)  # a bit inside the corner
    return bool(ctypes.windll.user32.MonitorFromPoint(pt, 0))  # 0 = MONITOR_DEFAULTTONULL


def check_for_update():
    """(version, download url) of a newer release on GitHub, or None."""
    import urllib.request
    req = urllib.request.Request(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                                 headers={"User-Agent": "HuntTracker", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=6) as r:
        rel = json.load(r)
    ver = str(rel.get("tag_name", "")).lstrip("vV")
    def parts(x):
        return tuple(int(n) for n in re.findall(r"\d+", x))
    if not ver or parts(ver) <= parts(VERSION):
        return None
    for a in rel.get("assets", []):
        if a.get("name", "").lower().endswith(".exe"):
            return ver, a["browser_download_url"]
    return None


def download_update(ver: str, url: str, progress=None) -> Path:
    import tempfile
    import urllib.request
    dest = Path(tempfile.gettempdir()) / f"HuntTrackerSetup-{ver}.exe"
    tmp = dest.with_suffix(".part")
    req = urllib.request.Request(url, headers={"User-Agent": "HuntTracker"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        total, done = int(r.headers.get("Content-Length") or 0), 0
        while chunk := r.read(1 << 16):
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    tmp.replace(dest)
    return dest


def clean_env() -> dict:
    """Environment for starting another copy of the .exe. Without this the new copy inherits this
    one's unpack folder, which is deleted when this one exits ("Failed to load Python DLL")."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("_PYI", "_MEIPASS"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def run_installer(setup: Path) -> None:
    """Install over this copy, showing only the installer's progress window. It closes the tracker
    and reopens it afterwards (/UPDATE=1)."""
    subprocess.Popen([str(setup), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE=1"],
                     env=clean_env(), creationflags=0x00000008 | 0x00000200)  # detached, own process group
    os._exit(0)


def startup_update_check() -> None:
    """Installed .exe: a small "Checking for updates" window before the tracker opens. A newer version
    is downloaded with a progress bar, then installed, and the tracker reopens by itself. Offline or up
    to date: the window closes and the tracker opens as usual."""
    import tkinter as tk
    from tkinter import ttk
    win = tk.Tk()
    win.title("Hunt Tracker - Update")
    win.configure(bg=C["bg"])
    win.resizable(False, False)
    w, h = 380, 130
    win.geometry(f"{w}x{h}+{(win.winfo_screenwidth() - w) // 2}+{(win.winfo_screenheight() - h) // 2}")
    win.attributes("-topmost", True)
    try:
        win.iconbitmap(default=sys.executable)
    except tk.TclError:
        pass
    msg = tk.Label(win, text="Checking for updates\u2026", bg=C["bg"], fg=C["cream"], font=(TEXT_FONT, 13, "bold"))
    msg.pack(pady=(22, 12))
    style = ttk.Style(win)
    style.theme_use("clam")
    style.configure("Hunt.Horizontal.TProgressbar", troughcolor=C["faint"], background=C["edge_hi"],
                    bordercolor=C["inner"], lightcolor=C["edge_hi"], darkcolor=C["blood"], thickness=14)
    bar = ttk.Progressbar(win, style="Hunt.Horizontal.TProgressbar", length=320, mode="indeterminate")
    bar.pack()
    bar.start(12)
    sub = tk.Label(win, text="", bg=C["bg"], fg=C["dim"], font=(TEXT_FONT, 9))
    sub.pack(pady=(8, 0))
    st = {"phase": "check", "done": 0, "total": 0}

    def work():
        try:
            found = check_for_update()
            if not found:
                st["phase"] = "none"
                return
            st["ver"] = found[0]
            st["phase"] = "download"
            st["setup"] = download_update(*found, progress=lambda d, t: st.update(done=d, total=t))
            st["phase"] = "install"
        except Exception:  # noqa: BLE001 - offline or GitHub down: just open the tracker
            st["phase"] = "none"

    def poll():
        ph = st["phase"]
        if ph == "none":
            win.destroy()
            return
        if ph == "download":
            if str(bar["mode"]) != "determinate":
                bar.stop()
                bar.configure(mode="determinate", maximum=100, value=0)
                msg.configure(text=f"Downloading update v{st['ver']}\u2026")
            if st["total"]:
                bar["value"] = st["done"] * 100 / st["total"]
                sub.configure(text=f"{st['done'] / 1e6:.1f} / {st['total'] / 1e6:.1f} MB")
        elif ph == "install":
            bar["value"] = 100
            msg.configure(text=f"Installing v{st['ver']}\u2026")
            sub.configure(text="Hunt Tracker opens by itself when it's done")
            win.after(700, lambda: run_installer(st["setup"]))
            return
        win.after(80, poll)

    threading.Thread(target=work, daemon=True).start()
    win.protocol("WM_DELETE_WINDOW", lambda: os._exit(0))
    poll()
    win.mainloop()


def run_window(t: Tracker) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError:
        fail("tkinter isn't available in this Python, so the window can't open.\n"
             "Reinstall Python from python.org and keep 'tcl/tk and IDLE' ticked,\n"
             "or use the browser version: py hunttracker.py --web")
    import ctypes
    ctypes.windll.kernel32.CreateMutexW(None, False, "HuntTrackerWindow")
    if ctypes.windll.kernel32.GetLastError() == 183:  # already open
        hw = ctypes.windll.user32.FindWindowW(None, "Hunt Tracker")
        if hw:
            ctypes.windll.user32.ShowWindow(hw, 9)
            ctypes.windll.user32.SetForegroundWindow(hw)
        return
    root = tk.Tk()
    root.title("Hunt Tracker")
    root.configure(bg=C["bg"])
    root.resizable(True, True)  # drag the edges like any window; everything scales to fit
    st = {"k": float(t.cfg.get("window_scale", 1.0)), "last": None, "size": None}
    top = tk.BooleanVar(value=bool(t.cfg.get("always_on_top", False)))
    detailed = tk.BooleanVar(value=bool(t.cfg.get("detailed", True)))
    career = tk.BooleanVar(value=t.cfg.get("page") == "career")
    players = tk.BooleanVar(value=bool(t.cfg.get("players_panel", True)))
    root.attributes("-topmost", top.get())
    # A slim toolbar across the top with the view toggles (replaces the old right-click menu).
    BAR_H = 30
    bar = tk.Frame(root, bg="#0b0908", height=BAR_H)
    bar.pack(fill="x", side="top")
    bar.pack_propagate(False)
    tk.Frame(root, bg=C["blood"], height=1).pack(fill="x", side="top")
    canvas = tk.Canvas(root, bg=C["bg"], highlightthickness=0, bd=0)
    canvas.pack(fill="both", expand=True)
    # Reopen at the last size and spot; the first time, at the scene's size times the old scale.
    W0, H0, _ = full_scene(t.view())
    size = t.cfg.get("window_size") or [int(W0 * st["k"]), int(H0 * st["k"])]
    pos = t.cfg.get("window_pos")
    root.geometry(f"{size[0]}x{size[1]}" + (f"+{pos[0]}+{pos[1]}" if pos and on_a_monitor(*pos) else ""))
    st["size"] = (size[0], max(50, size[1] - BAR_H - 1))  # the panels get the space under the toolbar

    def on_canvas(e):
        if (e.width, e.height) != st["size"]:
            st["size"] = (e.width, e.height)
            redraw(force=True)  # resized: rescale to the new size

    def remember_pos(e=None):
        if st.get("pos_job"):
            root.after_cancel(st["pos_job"])
        st["pos_job"] = root.after(800, save_pos)  # once the window stops moving / resizing

    def save_pos():
        st["pos_job"] = None
        p = [root.winfo_x(), root.winfo_y()]
        s = [root.winfo_width(), root.winfo_height()]
        if (p != t.cfg.get("window_pos") or s != t.cfg.get("window_size")) and on_a_monitor(*p):
            t.cfg["window_pos"], t.cfg["window_size"] = p, s
            save_cfg()

    def close(_e=None):
        save_pos()
        root.destroy()

    root.bind("<Configure>", remember_pos)
    canvas.bind("<Configure>", on_canvas)
    root.protocol("WM_DELETE_WINDOW", close)

    def save_cfg():
        t.cfg["window_scale"], t.cfg["always_on_top"] = round(st["k"], 2), bool(top.get())
        t.cfg["detailed"] = bool(detailed.get())
        t.cfg["page"] = "career" if career.get() else "tracker"
        t.cfg["players_panel"] = bool(players.get())
        try:
            CONFIG_PATH.write_text(json.dumps(t.cfg, indent=2), encoding="utf-8")
        except OSError:
            pass

    def redraw(force=False):
        v = t.view()
        cw, ch = st["size"]
        wide = cw > ch * 1.3  # landscape window: more, shorter columns, so text can be bigger
        W, H, ops = full_scene(v, wide=wide)
        panels = max(1, round(W / 360))
        # Text size: as big as fits the window's height (and no wider than it at the normal layout).
        k = max(0.3, min(cw / W, ch / H))
        # Then the panels stretch sideways to fill the rest of the width.
        panel_w = int(cw / k / panels)
        if panel_w > 360:
            W, H, ops = full_scene(v, panel_w, wide)
            k = max(0.3, min(k, ch / H))
        st["k"] = k
        key = (W, H, k, tuple(ops))
        if force or key != st["last"]:
            st["last"] = key
            boxes = tuple((round(o[1] * k), round(o[2] * k), round(o[3] * k), round(o[4] * k))
                          for o in ops if o[0] == "rect" and o[5] == C["panel"])
            ops = [("rect", *o[1:5], "none", o[6] or C["inner"], o[7]) if o[0] == "rect" and o[5] == C["panel"]
                   else o for o in ops]
            draw_ops(canvas, W, H, ops, k)
            if st.get("graph"):
                root.after_idle(place_graph)
            if True:  # the backdrop picture is always shown
                at = (0.5, 0.6)  # the hunter stays centred at every window size
                bkey = (cw, ch, at, boxes)
                if st.get("bd_key") != bkey:
                    data = backdrop_png(cw, ch, float(t.cfg.get("backdrop_opacity", 0.28)), at=at, panels=boxes)
                    st["bd_img"] = tk.PhotoImage(data=__import__("base64").b64encode(data)) if data else None
                    st["bd_key"] = bkey
                if st.get("bd_img"):
                    canvas.tag_lower(canvas.create_image(0, 0, image=st["bd_img"], anchor="nw"))

    def tick():
        try:
            t.cfg["detailed"] = bool(detailed.get())
            t.poll()
            redraw()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Hunt Tracker", f"Something went wrong:\n{e}")
            raise
        root.after(500, tick)

    def toggle_top():
        root.attributes("-topmost", top.get())
        save_cfg()

    def toggle_detail():
        t.cfg["detailed"] = bool(detailed.get())
        save_cfg()
        redraw(force=True)

    def refresh_view():
        save_cfg()
        redraw(force=True)
        paint_bar()

    # (label, setting, what to do after it flips, is it usable right now)
    toggles = [
        ("CAREER", career, refresh_view, lambda: True),
        ("PLAYER SUMMARY", players, refresh_view, lambda: True),
        ("DETAILED", detailed, lambda: (toggle_detail(), paint_bar()), lambda: True),
        ("ON TOP", top, lambda: (toggle_top(), paint_bar()), lambda: True),
    ]
    buttons = []

    def paint_bar(hover=None):
        for (label, var, _act, usable), b in zip(toggles, buttons):
            if not usable():
                b.configure(text="   " + label, fg="#3b342d", cursor="arrow")
            elif var.get():
                b.configure(text="\u25c6 " + label, fg=C["cream"] if b is hover else C["brass"], cursor="hand2")
            else:
                b.configure(text="\u25c7 " + label, fg=C["brass"] if b is hover else "#6f6456", cursor="hand2")

    def click(i):
        label, var, act, usable = toggles[i]
        if usable():
            var.set(not var.get())
            act()

    for i, _tg in enumerate(toggles):
        b = tk.Label(bar, bg="#0b0908", font=(TEXT_FONT, 10, "bold"), padx=12)
        b.pack(side="left", fill="y")
        b.bind("<Button-1>", lambda e, i=i: click(i))
        b.bind("<Enter>", lambda e, b=b: paint_bar(hover=b))
        b.bind("<Leave>", lambda e: paint_bar())
        buttons.append(b)
    paint_bar()

    def close_families(_e=None):
        p = st.pop("fam_pop", None)
        if p is not None and p.winfo_exists():
            p.destroy()

    def families_menu(e):
        """A checklist that stays open while you tick families on and off (a Tk menu closes on every click)."""
        if st.get("fam_pop") is not None and st["fam_pop"].winfo_exists():
            close_families()  # clicking the heading again closes it
            return
        fams = (load_career() or {}).get("weapon_families", {})
        pop = tk.Toplevel(root, bg=C["blood"], padx=1, pady=1)
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        st["fam_pop"] = pop
        body = tk.Frame(pop, bg=C["panel"], padx=10, pady=8)
        body.pack(fill="both", expand=True)
        head = tk.Frame(body, bg=C["panel"])
        head.pack(fill="x", pady=(0, 6))
        rows = {}

        def paint():
            hidden = set(t.cfg.get("hidden_families") or [])
            for name, lab in rows.items():
                on = name not in hidden
                lab.configure(text=("◆ " if on else "◇ ") + lab.base,
                              fg=C["cream"] if on else "#6f6456")

        def set_hidden(new):
            t.cfg["hidden_families"] = sorted(new)
            save_cfg()
            paint()
            redraw(force=True)

        def link(parent, text, cmd, side="left"):
            b = tk.Label(parent, text=text, bg=C["panel"], fg=C["brass"], font=(TEXT_FONT, 9, "bold"),
                         cursor="hand2", padx=6)
            b.pack(side=side)
            b.bind("<Button-1>", lambda _e: cmd())
            b.bind("<Enter>", lambda _e: b.configure(fg=C["cream"]))
            b.bind("<Leave>", lambda _e: b.configure(fg=C["brass"]))

        link(head, "DONE ✕", close_families, side="right")
        if not fams:
            tk.Label(body, text="No families read yet: open your weapon kills in Hunt", bg=C["panel"],
                     fg=C["dim"], font=(TEXT_FONT, 10)).pack()
        else:
            link(head, "SHOW ALL", lambda: set_hidden(set()))
            link(head, "HIDE ALL", lambda: set_hidden(set(fams)))
            grid = tk.Frame(body, bg=C["panel"])
            grid.pack(fill="both")
            items = sorted(fams.items(), key=lambda kv: (-kv[1], kv[0]))
            per_col = max(1, -(-len(items) // 2))  # two columns
            for i, (name, n) in enumerate(items):
                lab = tk.Label(grid, bg=C["panel"], font=(TEXT_FONT, 10), anchor="w", cursor="hand2", padx=4)
                lab.base = f"{name}  ({n:,})"
                lab.grid(row=i % per_col, column=i // per_col, sticky="w", padx=(0, 14))
                lab.bind("<Button-1>", lambda _e, name=name: set_hidden(
                    set(t.cfg.get("hidden_families") or []) ^ {name}))
                rows[name] = lab
            paint()
        pop.bind("<Escape>", close_families)
        pop.update_idletasks()
        x = min(e.x_root, root.winfo_screenwidth() - pop.winfo_reqwidth() - 8)
        y = min(e.y_root + 12, root.winfo_screenheight() - pop.winfo_reqheight() - 48)
        pop.geometry(f"+{max(0, x)}+{max(0, y)}")
        pop.focus_force()

    root.bind("<Unmap>", close_families, add="+")

    def hot_at(x, y):
        return next((z[4] for z in getattr(canvas, "hot", []) if z[0] <= x <= z[2] and z[1] <= y <= z[3]), None)

    def on_motion(e):
        if canvas.find_withtag("current") and "famhead" in canvas.gettags("current"):
            return
        canvas.configure(cursor="hand2" if hot_at(e.x, e.y) else "")

    def on_click(e):
        key = hot_at(e.x, e.y)
        if key:
            open_graph(key)
        elif st.get("graph"):
            close_graph()  # a click anywhere else folds the graph away, like a menu

    canvas.bind("<Motion>", on_motion)
    canvas.bind("<Button-1>", on_click)
    root.bind("<Escape>", lambda e: close_graph() if st.get("graph") else close(e))

    def graph_area():
        """Where the graph pops out: over the Lifetime list, from its first row to its footnotes."""
        zones = getattr(canvas, "hot", [])
        if not zones:
            return None
        x0, x1 = min(z[0] for z in zones), max(z[2] for z in zones)
        y0, y1 = min(z[1] for z in zones), max(z[3] for z in zones)
        y1 += (y1 - y0) / len(zones) * 2  # the two footnote lines under the list
        cw, ch = canvas.winfo_width(), canvas.winfo_height()
        x0, y0, x1, y1 = max(0, x0 - 4), max(0, y0 - 6), min(cw, x1 + 4), min(ch, y1)
        return int(x0), int(y0), int(x1 - x0), int(y1 - y0)

    def close_graph():
        g = st.pop("graph", None)
        if g and g["frame"].winfo_exists():
            g["frame"].destroy()

    def place_graph():
        g, area = st.get("graph"), graph_area()
        if not g:
            return
        if not area:  # the Lifetime list isn't on screen any more (Career page, Detailed off)
            close_graph()
            return
        x, y, w, h = area
        g["frame"].place(x=x, y=y, width=w, height=max(1, int(h * g["unfold"])))

    def open_graph(key):
        """One lifetime stat drawn day by day, popped out over the Lifetime list. Hover a day to read
        its value; the arrows step through the other stats."""
        g = st.get("graph")
        if g:
            g["key"] = key
            g["draw"]()
            return
        frame = tk.Frame(canvas, bg=C["blood"])
        cv = tk.Canvas(frame, bg=C["panel"], highlightthickness=0, bd=0)
        cv.pack(fill="both", expand=True, padx=1, pady=1)
        g = {"key": key, "hover": None, "frame": frame, "unfold": 0.15}
        st["graph"] = g
        keys = list(STAT_GRAPHS)

        def step(d):
            g["key"] = keys[(keys.index(g["key"]) + d) % len(keys)]
            g["hover"] = None
            draw()

        def button(x, y, txt, anchor, cmd, size=13):
            it = cv.create_text(x, y, text=txt, anchor=anchor, fill=C["brass"], font=(TEXT_FONT, size, "bold"))
            bx = cv.bbox(it)
            hit = cv.create_rectangle(bx[0] - 6, bx[1] - 4, bx[2] + 6, bx[3] + 4, fill=C["panel"], outline="")
            cv.tag_lower(hit, it)
            for i_ in (it, hit):
                cv.tag_bind(i_, "<Button-1>", lambda _e: cmd())
                cv.tag_bind(i_, "<Enter>", lambda _e: (cv.itemconfigure(it, fill=C["cream"]),
                                                       cv.configure(cursor="hand2")))
                cv.tag_bind(i_, "<Leave>", lambda _e: (cv.itemconfigure(it, fill=C["brass"]),
                                                       cv.configure(cursor="")))
            return bx

        def draw():
            key = g["key"]
            title, kind, fmt = STAT_GRAPHS[key]
            cv.delete("all")
            days, series, hi = stat_series(t, key)
            Wd, Hd = max(cv.winfo_width(), 200), max(cv.winfo_height(), 120)
            sc = max(0.6, min(1.4, Wd / 420))  # text grows with the window like the rest of the tracker
            L, R, T, B = int(54 * sc), int(12 * sc), int(52 * sc), int(26 * sc)
            pw, ph = Wd - L - R, Hd - T - B

            # header: title, the arrows and close
            bx = button(Wd - 8, 6, "\u2715", "ne", close_graph, int(12 * sc))
            bx = button(bx[0] - 14, 4, "\u25b8", "ne", lambda: step(1), int(14 * sc))
            bx = button(bx[0] - 10, 4, "\u25c2", "ne", lambda: step(-1), int(14 * sc))

            def fit(item, right):  # shorten a line of text with "\u2026" until it ends before `right`
                txt = cv.itemcget(item, "text")
                while cv.bbox(item)[2] > right and len(txt) > 4:
                    txt = txt[:-2]
                    cv.itemconfigure(item, text=txt.rstrip(" \u00b7(") + "\u2026")

            cv.create_polygon(10, 14 * sc, 14, 10 * sc, 18, 14 * sc, 14, 18 * sc, fill=C["blood"], outline="")
            fit(cv.create_text(24, 6, text=title.upper(), anchor="nw", fill=C["brass"],
                               font=(TEXT_FONT, int(11 * sc), "bold")), bx[0] - 12)
            vals = [x for _, _, vs in series for x in vs if x is not None]
            real = [x for x in series[0][2] if x is not None]
            sub = f"{len(days)} days since {days[0].strftime('%b %d')}"
            if real and kind == "bar":
                sub += f"  \u00b7  total {fmt(sum(real))}  \u00b7  best day {fmt(max(real))}"
            elif real:
                sub += f"  \u00b7  now {fmt(real[-1])}  \u00b7  high {fmt(max(real))}  \u00b7  low {fmt(min(real))}"
            fit(cv.create_text(24, 6 + 18 * sc, text=sub, anchor="nw", fill=C["dim"], font=(TEXT_FONT, int(9 * sc))),
                Wd - 8)
            lx = Wd - R
            for nm, col, _ in reversed(series) if len(series) > 1 else []:  # legend, in each line's colour
                it = cv.create_text(lx, T - 14 * sc, text=f"\u25c6 {nm}", anchor="se", fill=col,
                                    font=(TEXT_FONT, int(9 * sc)))
                lx = cv.bbox(it)[0] - 12

            lo, top_v = 0, max(vals or [1])
            if key in ("level", "steam") and vals:  # these only climb: zoom in on the part that moved
                lo = min(vals)
                pad_ = max((top_v - lo) * 0.1, 1)
                lo, top_v = max(0, lo - pad_), top_v + pad_
            else:
                top_v = top_v * 1.1 or 1
            span = (top_v - lo) or 1

            def ypos(x):
                return T + ph - (x - lo) / span * ph

            n = len(days)
            slot = pw / max(n, 1)

            def xpos(i):
                return L + slot * i + slot / 2

            def label(x):
                return fmt(x if kind == "line" or key in ("time_hunting", "time_in_game") else round(x))

            for i in range(4):  # gridlines and their values
                v_ = lo + span * i / 3
                yy = ypos(v_)
                cv.create_line(L, yy, Wd - R, yy, fill=C["faint"])
                cv.create_text(L - 6, yy, text=label(v_), anchor="e", fill=C["dim"], font=(NUM_FONT, int(9 * sc)))
            cv.create_line(L, T + ph, Wd - R, T + ph, fill=C["bar"])
            step_ = max(1, round(n / max(2, pw // int(62 * sc))))
            for i in range(0, n, step_):
                cv.create_text(xpos(i), T + ph + 5, text=days[i].strftime("%b %d"), anchor="n",
                               fill=C["dim"], font=(NUM_FONT, int(9 * sc)))
            for nm, col, vs in series:
                if kind == "bar":
                    bw = max(1, slot * 0.72)
                    for i, x in enumerate(vs):
                        if x:
                            c_ = C["gold"] if hi == i else (C["bar"] if hi is not None else col)
                            cv.create_rectangle(xpos(i) - bw / 2, ypos(x), xpos(i) + bw / 2, T + ph, fill=c_,
                                                outline="")
                else:
                    pts = [(xpos(i), ypos(x)) for i, x in enumerate(vs) if x is not None]
                    if len(pts) > 1:
                        cv.create_line(*[c for p_ in pts for c in p_], fill=col, width=2)
                    r_ = 2.5 if n <= 120 else 1.5
                    for px, py in pts:
                        cv.create_oval(px - r_, py - r_, px + r_, py + r_, fill=col, outline="")
            g.update(days=days, series=series, xpos=xpos, slot=slot, L=L, R=R, T=T, ph=ph, Wd=Wd, sc=sc,
                     label=label)
            show_hover()

        def show_hover():
            cv.delete("hover")
            i = g["hover"]
            if i is None or "days" not in g or not (0 <= i < len(g["days"])):
                return
            x = g["xpos"](i)
            cv.create_line(x, g["T"], x, g["T"] + g["ph"], fill=C["brass"], dash=(3, 3), tags="hover")
            parts = [g["days"][i].strftime("%a %b %d")]
            for nm, _, vs in g["series"]:
                val = vs[i]
                parts.append((f"{nm}: " if len(g["series"]) > 1 else "") + ("-" if val is None else g["label"](val)))
            anchor = "nw" if x < g["Wd"] / 2 else "ne"
            item = cv.create_text(x + 8 if anchor == "nw" else x - 8, g["T"] + 4, text="   ".join(parts),
                                  anchor=anchor, fill=C["cream"], font=(TEXT_FONT, int(10 * g["sc"]), "bold"),
                                  tags="hover")
            bx = cv.bbox(item)
            cv.tag_lower(cv.create_rectangle(bx[0] - 5, bx[1] - 3, bx[2] + 5, bx[3] + 3, fill=C["bg"],
                                             outline=C["inner"], tags="hover"), item)

        def on_move(e):
            if "days" not in g:
                return
            i = int((e.x - g["L"]) // g["slot"]) if g["slot"] else None
            ok = i is not None and 0 <= i < len(g["days"]) and e.x <= g["Wd"] - g["R"] and e.y > g["T"] - 6
            i = i if ok else None
            if i != g["hover"]:
                g["hover"] = i
                show_hover()

        def on_leave(_e):
            g["hover"] = None
            show_hover()

        def unfold():  # opens downward like a menu, in a few quick steps
            if st.get("graph") is not g:
                return
            g["unfold"] = min(1.0, g["unfold"] + 0.17)
            place_graph()
            if g["unfold"] < 1.0:
                root.after(12, unfold)

        g["draw"] = draw
        cv.bind("<Configure>", lambda _e: draw())
        cv.bind("<Motion>", on_move)
        cv.bind("<Leave>", on_leave)
        place_graph()
        unfold()

    canvas.tag_bind("famhead", "<Button-1>", families_menu)
    canvas.tag_bind("famhead", "<Enter>", lambda e: canvas.configure(cursor="hand2"))
    canvas.tag_bind("famhead", "<Leave>", lambda e: canvas.configure(cursor=""))
    note = tk.Label(bar, bg="#0b0908", fg="#6f6456", font=(TEXT_FONT, 9), padx=12, text=f"v{VERSION}")
    note.pack(side="right", fill="y")

    redraw(force=True)
    tick()
    root.mainloop()


def run_web(t: Tracker) -> None:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/scene.svg":
                body, ctype = build_svg(t.view()).encode(), "image/svg+xml"
            elif path == "/":
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    host, port = t.cfg["host"], int(t.cfg["port"])
    try:
        srv = ThreadingHTTPServer((host, port), H)
    except OSError as e:
        fail(f"Can't start on {host}:{port}: {e}\nIs it already running? Or change \"port\" in hunttracker.json.")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://localhost:{port}"
    print(f"Hunt Tracker is running: {url}")
    if host != "127.0.0.1":
        print(f"On your phone/tablet (same Wi-Fi): http://{lan_ip()}:{port}")
    print("Leave this window open. Drag the browser tab to your second monitor. Ctrl+C to stop.\n")
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    try:
        while True:
            t.poll()
            time.sleep(float(t.cfg["poll_seconds"]))
    except KeyboardInterrupt:
        print("Bye.")


def run_console(t: Tracker) -> None:
    last = None
    try:
        while True:
            t.poll()
            v = t.view()
            line = (f"{STATES[v['state']][0]:<24} hunt {fmt_dur(v['hunt_elapsed']) if v['hunt_elapsed'] is not None else '-':>7} | "
                    f"today {v['today_n']} | all-time {v['all_hunts']} | avg {fmt_dur(v['avg']) if v['avg'] else '-'} | level {v['level']}"
                    )
            if line != last:
                print(time.strftime("[%H:%M:%S] ") + line)
                last = line
            time.sleep(float(t.cfg["poll_seconds"]))
    except KeyboardInterrupt:
        print("Bye.")


def run_watcher() -> None:
    """Started with Windows (Startup folder): opens the tracker each time Hunt starts."""
    import ctypes
    ctypes.windll.kernel32.CreateMutexW(None, False, "HuntTrackerWatcher")
    if ctypes.windll.kernel32.GetLastError() == 183:  # another watcher is already running
        return
    cmd = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve())]
    def hunt_up() -> bool:
        try:
            return "HuntGame.exe" in subprocess.run(["tasklist", "/FI", "IMAGENAME eq HuntGame.exe", "/NH"],
                                                    capture_output=True, text=True, timeout=5,
                                                    creationflags=0x08000000).stdout
        except Exception:  # noqa: BLE001 - unsure counts as "not running" here
            return False

    armed = True
    while True:
        if hunt_up():
            if armed and not ctypes.windll.user32.FindWindowW(None, "Hunt Tracker"):
                subprocess.Popen(cmd, cwd=str(HERE), creationflags=0x08000000, env=clean_env())
            armed = False  # closing the tracker mid-session keeps it closed until Hunt restarts
        else:
            armed = True
        time.sleep(5)


def main() -> None:
    # The windowed .exe has no console: print() would fail, so its output goes nowhere instead.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if "--watch" in sys.argv:
        run_watcher()
        return
    ap = argparse.ArgumentParser()
    ap.add_argument("--console", action="store_true")
    ap.add_argument("--web", action="store_true", help="browser-tab version")
    ap.add_argument("--log")
    ap.add_argument("--svg", help="write one snapshot to this file and exit (for testing)")
    args = ap.parse_args()
    cfg = load_config()
    if FROZEN and not (args.console or args.web or args.svg):
        import ctypes
        hw = ctypes.windll.user32.FindWindowW(None, "Hunt Tracker")
        if hw:  # already open: bring it forward instead
            ctypes.windll.user32.ShowWindow(hw, 9)
            ctypes.windll.user32.SetForegroundWindow(hw)
            return
        if cfg.get("auto_update", True):
            startup_update_check()
    if args.log:
        cfg["log_path"] = args.log
    log = find_log(cfg) or (None if args.svg else ask_for_log())
    if log and str(log) != cfg.get("log_path") and not args.svg:
        cfg["log_path"] = str(log)
        try:
            CONFIG_PATH.write_text(json.dumps({k: cfg[k] for k in DEFAULTS}, indent=2), encoding="utf-8")
        except OSError:
            pass
    if not log:
        fail("Couldn't find Hunt's Game.log.\nIt's in your Hunt folder, in the USER folder, e.g.\n"
             "  D:\\Steam\\steamapps\\common\\Hunt Showdown 1896\\USER\\Game.log\n"
             "Put the full path in hunttracker.json as \"log_path\".")
    t = Tracker(cfg, log)
    c = load_career()
    if c and c.get("profile_stats"):  # stats hovered in Profile > Statistic before they were shown
        merge_stats(list(c["profile_stats"].items()), picker=True, only_missing=True)
    for r in reversed(t.hist.get("reads", [])):  # your squad-page column read before it was logged
        mine = [p for p in r.get("players", []) if r["kind"] in ("lobby", "cards") and t._key(p["name"]) == t._me()]
        if mine and t._me():
            t.note_my_stats(mine[0].get("stats", {}))
            break
    t.poll()
    if not args.svg:
        start_summary_capture(t)
        start_map_timer(t)
    if args.svg:
        Path(args.svg).write_text(build_svg(t.view()), encoding="utf-8")
        print(json.dumps(t.view(), default=str, indent=1))
        return
    (run_console if args.console else run_web if args.web else run_window)(t)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        pass
    except Exception:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        try:
            (DATA / "crash.log").write_text(tb, encoding="utf-8")
        except OSError:
            pass
        fail("Hunt Tracker crashed. Details (also saved to crash.log):\n\n" + tb)
