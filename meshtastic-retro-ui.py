#!/usr/bin/env python3
"""
meshtastic-retro-ui.py

Retro Meshtastic Badge – 3.5″ Touch, Headless Edition
• Full-width title bar with link status in color
• Scrollable message list (touch, wheel, keys)
• Send mode (S → type → Enter)
• Quit with Ctrl-C or Q
• Persists to ~/.retrobadge/{meshtastic.db,meshtastic.log}
"""

import os, json, sqlite3, signal, queue, threading, time, curses
from pathlib import Path
from datetime import datetime
import getpass
import sys

import meshtastic.serial_interface as mserial     # from venv
from pubsub import pub                            # from venv

# ── CONFIG ───────────────────────────────────────────────────────────────────
DEV_PATH = os.getenv("MESHTASTIC_DEV", "/dev/rfcomm0")
BAUD     = int(os.getenv("MESHTASTIC_BAUD", "921600"))  # Add this line
DATA_DIR = Path.home() / ".retrobadge"; DATA_DIR.mkdir(exist_ok=True)
DB_FILE  = DATA_DIR / "meshtastic.db"
LOG_FILE = DATA_DIR / "meshtastic.log"

MAX_LEN, PAD_V = 240, 2            # msg truncate, vertical padding

# ── PERSISTENCE ───────────────────────────────────────────────────────────────
json_fh = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
db      = sqlite3.connect(DB_FILE, check_same_thread=False)
with db:
    db.execute("""
      CREATE TABLE IF NOT EXISTS messages (
        ts   REAL,
        src  TEXT,
        txt  TEXT
      )""")

# ── SHARED STATE ───────────────────────────────────────────────────────────────
incoming_q  = queue.Queue(1024)
outgoing_q  = queue.Queue(256)
link_up_evt = threading.Event()
stop_evt    = threading.Event()
_iface_lock = threading.Lock()
_iface      = None

# ── PubSub callbacks ──────────────────────────────────────────────────────────
def _handle_text(pkt, _iface):
    ts  = getattr(pkt, "rxTime", pkt.get("timestamp", time.time()))
    if ts > 1e12: ts /= 1000
    src = getattr(pkt, "fromId", pkt.get("from", {}).get("userAlias", "unknown"))
    txt = (pkt.decoded.text if hasattr(pkt, "decoded") else pkt["decoded"]["text"])[:MAX_LEN]
    json_fh.write(json.dumps(pkt, default=str) + "\n")
    with db:
        db.execute("INSERT INTO messages VALUES (?,?,?)", (ts, src, txt))
    incoming_q.put((ts, src, txt))

pub.subscribe(_handle_text,        "meshtastic.receive.text")
pub.subscribe(lambda _: link_up_evt.set(),   "meshtastic.connection.established")
pub.subscribe(lambda _: link_up_evt.clear(), "meshtastic.connection.lost")

# ── RADIO THREAD ──────────────────────────────────────────────────────────────
def _radio_worker():
    global _iface
    while not stop_evt.is_set():
        try:
            # Log connection attempt
            json_fh.write(f"# Trying {DEV_PATH} at {BAUD} baud\n")
            iface = mserial.SerialInterface(devPath=DEV_PATH, baud=BAUD)  # Add baud param
            if not iface.waitForConfig():
                raise RuntimeError("Node config timeout")
            with _iface_lock:
                _iface = iface

            # dispatch outbound messages
            while not stop_evt.wait(0.1):
                try:
                    msg = outgoing_q.get_nowait()
                    iface.sendText(msg)
                except queue.Empty:
                    pass

        except Exception as e:
            json_fh.write(f"# radio error: {e}\n")
            link_up_evt.clear()
            time.sleep(2)
        finally:
            with _iface_lock:
                if _iface:
                    try: _iface.close()
                    except: pass
                _iface = None

# ── HELPERS ──────────────────────────────────────────────────────────────────
def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")

def _history(limit=2000):
    cur = db.cursor()
    cur.execute("SELECT ts, src, txt FROM messages ORDER BY ts DESC LIMIT ?", (limit,))
    return list(reversed(cur.fetchall()))

def safe_footer(win, row: int, text: str, attr=0):
    """Write at (row,0) clipped to width-1 to avoid bottom-right ERR."""
    h, w = win.getmaxyx()
    safe = text.ljust(w-1)[:w-1]
    try:
        win.addstr(row, 0, safe, attr)
    except curses.error:
        pass

# ── CURSES UI ─────────────────────────────────────────────────────────────────
def _ui(stdscr):
    curses.curs_set(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.start_color()
    curses.use_default_colors()

    # Colour pairs
    curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)  # text
    curses.init_pair(2, curses.COLOR_RED,   curses.COLOR_BLACK)  # NO LINK
    curses.init_pair(3, curses.COLOR_BLUE,  curses.COLOR_BLACK)  # LINK

    text_col   = curses.color_pair(1)
    no_link    = curses.color_pair(2)
    yes_link   = curses.color_pair(3)

    msgs      = _history()
    viewofs   = max(0, len(msgs) - (curses.LINES - PAD_V*2 - 2))
    send_mode = False
    inp       = ""

    TITLE  = " Retro-Meshtastic Badge — Touch or ↑/↓ to scroll "

    while not stop_evt.is_set():
        # Drain incoming
        try:
            while True:
                msgs.append(incoming_q.get_nowait())
        except queue.Empty:
            pass

        h, w   = stdscr.getmaxyx()
        pane_h = h - PAD_V*2 - 2   # header=2 rows, footer=2 rows

        # Auto-tail if at bottom
        if viewofs >= max(0, len(msgs) - pane_h):
            viewofs = max(0, len(msgs) - pane_h)

        stdscr.erase()

        # ── Row 0: full-width title bar ──────────────────────────────
        stdscr.addstr(0, 0,
                      "╔" + TITLE.center(w-2, "═")[:w-2] + "╗",
                      text_col)

        # ── Row 1: link status, plain text, colored ──────────────────
        status = "[● LINKED]" if link_up_evt.is_set() else "[○ NO LINK]"
        status_attr = yes_link if link_up_evt.is_set() else no_link
        safe_footer(stdscr, 1, status.center(w-1), status_attr)

        # ── Messages start at row 2 ─────────────────────────────────
        for i in range(pane_h):
            idx = viewofs + i
            if idx >= len(msgs):
                break
            ts, src, txt = msgs[idx]
            prefix = f"{_fmt(ts)} {src[:10]:>10} │ "
            avail  = w - len(prefix)
            line   = (prefix + txt[:avail]).ljust(w)[:w]
            stdscr.addstr(PAD_V + 2 + i, 0, line, text_col)

        # ── Footer (rows h-2 & h-1) ────────────────────────────────
        stdscr.addstr(h-2, 0, "╚" + "═"*(w-2) + "╝", text_col)
        if send_mode:
            prompt = f"Send> {inp}"
            safe_footer(stdscr, h-1, prompt, text_col)
            stdscr.move(h-1, min(len(prompt), w-2))
        else:
            footer = "[S]end  [Ctrl-C/Q] quit  ↑/↓ PgUp/PgDn  Touch scroll"
            safe_footer(stdscr, h-1, footer, text_col)

        stdscr.refresh()
        curses.napms(30)

        # ── Input handling ─────────────────────────────────────────
        try:
            c = stdscr.getch()
        except curses.error:
            continue

        if send_mode:
            if c in (10, 13):
                msg = inp.strip()
                send_mode = False
                inp = ""
                if msg:
                    ts = time.time()
                    with db:
                        db.execute("INSERT INTO messages VALUES (?,?,?)",
                                   (ts, "You", msg))
                    msgs.append((ts, "You", msg))
                    outgoing_q.put(msg)
            elif c in (27,):
                send_mode = False
            elif c in (127, 8):
                inp = inp[:-1]
            elif 32 <= c <= 126:
                inp += chr(c)
            continue

        # ── Navigation mode ───────────────────────────────────────
        if c == curses.KEY_UP:
            viewofs = max(0, viewofs - 1)
        elif c == curses.KEY_DOWN:
            viewofs = min(len(msgs)-pane_h, viewofs + 1)
        elif c == curses.KEY_PPAGE:
            viewofs = max(0, viewofs - pane_h)
        elif c == curses.KEY_NPAGE:
            viewofs = min(len(msgs)-pane_h, viewofs + pane_h)
        elif c == curses.KEY_MOUSE:
            _, mx, my, _, b = curses.getmouse()
            if b & curses.BUTTON4_PRESSED:
                viewofs = max(0, viewofs - 3)
            if b & curses.BUTTON5_PRESSED:
                viewofs = min(len(msgs)-pane_h, viewofs + 3)
            if b & curses.BUTTON1_PRESSED:
                drag_start = my
            if b & curses.BUTTON1_RELEASED:
                delta = my - drag_start
                viewofs = max(0, min(len(msgs)-pane_h, viewofs - delta))
        elif c in (ord('s'), ord('S')):
            send_mode = True
            inp = ""
        elif c in (ord('q'), ord('Q')):
            stop_evt.set()

# ── Entrypoint ───────────────────────────────────────────────────────────────
def _sig(*_): stop_evt.set()

def main():
    # Diagnostics
    json_fh.write(f"# Running as user: {getpass.getuser()}\n")
    json_fh.write(f"# Python executable: {sys.executable}\n")
    json_fh.write(f"# Checking /dev/rfcomm0 permissions...\n")
    try:
        st = os.stat(DEV_PATH)
        json_fh.write(f"# /dev/rfcomm0 mode: {oct(st.st_mode)} owner: {st.st_uid} group: {st.st_gid}\n")
    except Exception as e:
        json_fh.write(f"# Could not stat /dev/rfcomm0: {e}\n")

    signal.signal(signal.SIGINT,  _sig)
    signal.signal(signal.SIGTERM, _sig)
    threading.Thread(target=_radio_worker, daemon=True).start()
    curses.wrapper(_ui)
    stop_evt.set()
    json_fh.close()
    db.close()

if __name__ == "__main__":
    main()
