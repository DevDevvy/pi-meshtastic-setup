#!/usr/bin/env python3
"""
Retro Meshtastic Badge – 3.5″ Touch, Headless Edition
 • Runs in a plain terminal (curses)
 • Connects to /dev/rfcomm0 via Meshtastic‑Python
 • Scroll with finger (drag) or mouse‑wheel or ↑/↓/PgUp/PgDn
 • Press S → type message → Enter to send
 • Ctrl‑C (or Q) to quit gracefully
 • Persists all traffic to ~/.retrobadge/meshtastic.db  (SQLite)
"""

###############################################################################
# Imports & constants
###############################################################################
import os, json, sqlite3, signal, queue, threading, time, curses
from pathlib import Path
from datetime import datetime

import meshtastic.serial_interface  as mserial   # pip install meshtastic
from pubsub import pub                           # pip install pubsub

DEV_PATH  = "/dev/rfcomm0"                       # override via MESHTASTIC_DEV
DEV_PATH  = os.getenv("MESHTASTIC_DEV", DEV_PATH)

DATA_DIR  = Path.home() / ".retrobadge"; DATA_DIR.mkdir(exist_ok=True)
DB_FILE   = DATA_DIR / "meshtastic.db"
LOG_FILE  = DATA_DIR / "meshtastic.log"

THEME_FG, THEME_BG = curses.COLOR_GREEN, curses.COLOR_BLACK
MAX_LEN, PAD_V     = 240, 2                      # chars shown, padding rows

###############################################################################
# Persistence
###############################################################################
json_fh = open(LOG_FILE, "a", encoding="utf‑8", buffering=1)
db      = sqlite3.connect(DB_FILE, check_same_thread=False)
with db:
    db.execute("""
      CREATE TABLE IF NOT EXISTS messages (
        ts   REAL,
        src  TEXT,
        txt  TEXT
      )""")

###############################################################################
# Thread‑safe state
###############################################################################
incoming_q  = queue.Queue(1024)
outgoing_q  = queue.Queue(256)
link_up_evt = threading.Event()
stop_evt    = threading.Event()
_iface_lock = threading.Lock()
_iface      = None

###############################################################################
# PubSub callbacks  (Meshtastic‑Python ≥ 2.7.x)
###############################################################################
def _pkt_text(pkt, _iface):
    """Store & queue every text message."""
    ts  = getattr(pkt, "rxTime", pkt.get("timestamp", time.time()))
    if ts > 1e12: ts /= 1000
    src = getattr(pkt, "fromId", pkt.get("from", {}).get("userAlias", "unknown"))
    txt = (pkt.decoded.text if hasattr(pkt, "decoded") else
           pkt["decoded"]["text"])[:MAX_LEN]

    json_fh.write(json.dumps(pkt, default=str) + "\n")
    with db: db.execute("INSERT INTO messages VALUES (?,?,?)", (ts, src, txt))
    incoming_q.put((ts, src, txt))

pub.subscribe(_pkt_text,        "meshtastic.receive.text")
pub.subscribe(lambda _: link_up_evt.set(),   "meshtastic.connection.established")
pub.subscribe(lambda _: link_up_evt.clear(), "meshtastic.connection.lost")

###############################################################################
# Radio I/O thread
###############################################################################
def _radio():
    global _iface
    while not stop_evt.is_set():
        try:
            iface = mserial.SerialInterface(devPath=DEV_PATH)
            if not iface.waitForConfig():
                raise RuntimeError("Node config timeout")
            with _iface_lock: _iface = iface

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

###############################################################################
# Helpers
###############################################################################
def _fmt(ts):          return datetime.fromtimestamp(ts).strftime("%H:%M")
def _history(limit=2000):
    cur = db.cursor()
    cur.execute("SELECT ts, src, txt FROM messages ORDER BY ts DESC LIMIT ?", (limit,))
    return list(reversed(cur.fetchall()))

###############################################################################
# Curses UI
###############################################################################
# ── utility: never overflow bottom‑right ────────────────────────────
def safe_footer(win, row, text, attr):
    """Write a footer line without touching the last screen cell."""
    h, w = win.getmaxyx()
    safe = text.ljust(w - 1)[: w - 1]   # <=  w‑1 chars
    try:
        win.addstr(row, 0, safe, attr)
    except curses.error:
        pass                             # ignore if terminal resizes mid‑draw
def _ui(stdscr):
    curses.curs_set(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.start_color(); curses.use_default_colors()
    curses.init_pair(1, THEME_FG, THEME_BG)
    colour = curses.color_pair(1)

    msgs      = _history()
    viewofs   = max(0, len(msgs) - (curses.LINES - PAD_V*2))
    send_mode = False; inp = ""

    HEADER = "╔═ Retro‑Badge — Meshtastic ═╗"
    FOOTER = "[S]end  [Q]uit  ↑/↓ PgUp/PgDn Touch scroll"

    while not stop_evt.is_set():
        # drain new packets
        try:
            while True: msgs.append(incoming_q.get_nowait())
        except queue.Empty:
            pass

        h, w   = stdscr.getmaxyx()
        pane_h = h - PAD_V*2
        if viewofs >= max(0, len(msgs) - pane_h):
            viewofs = max(0, len(msgs) - pane_h)

        stdscr.erase()
        link = "[● LINK]" if link_up_evt.is_set() else "[○ NO LINK]"
        stdscr.addstr(0, 0, f"{HEADER} {link}".ljust(w)[:w], colour)

        # message list
        for i in range(pane_h):
            j = viewofs + i
            if j >= len(msgs): break
            ts, src, txt = msgs[j]
            pre   = f"{_fmt(ts)} {src[:10]:>10} │ "
            avail = w - len(pre)
            stdscr.addstr(PAD_V+i, 0, (pre + txt[:avail]).ljust(w)[:w], colour)

        # footer or prompt
        if send_mode:
            prompt = f"Send> {inp}"
            safe_footer(stdscr, h - 1, prompt, colour)
            stdscr.move(h-1, min(len(prompt), w-1))
        else:
            safe_footer(stdscr, h - 1, FOOTER, colour)

        stdscr.refresh(); curses.napms(30)

        try: ch = stdscr.getch()
        except curses.error: continue

        # ===== send prompt =====
        if send_mode:
            if ch in (10, 13):
                text = inp.strip(); inp = ""; send_mode = False
                if text:
                    ts = time.time()
                    with db: db.execute("INSERT INTO messages VALUES (?,?,?)",
                                        (ts, "You", text))
                    msgs.append((ts, "You", text))
                    outgoing_q.put(text)
            elif ch in (27,):          send_mode = False
            elif ch in (127, 8):       inp = inp[:-1]
            elif 32 <= ch <= 126:      inp += chr(ch)
            continue

        # ===== view mode =====
        if ch == curses.KEY_UP:            viewofs = max(0, viewofs-1)
        elif ch == curses.KEY_DOWN:        viewofs = min(len(msgs)-pane_h, viewofs+1)
        elif ch == curses.KEY_PPAGE:       viewofs = max(0, viewofs-pane_h)
        elif ch == curses.KEY_NPAGE:       viewofs = min(len(msgs)-pane_h, viewofs+pane_h)
        elif ch == curses.KEY_MOUSE:
            _, mx, my, _, b = curses.getmouse()
            if b & curses.BUTTON4_PRESSED: viewofs = max(0, viewofs-3)
            if b & curses.BUTTON5_PRESSED: viewofs = min(len(msgs)-pane_h, viewofs+3)
            if b & curses.BUTTON1_PRESSED: drag = my
            if b & curses.BUTTON1_RELEASED:
                viewofs = max(0, min(len(msgs)-pane_h, viewofs - (my-drag)))
        elif ch in (ord('s'), ord('S')):   send_mode, inp = True, ""
        elif ch in (ord('q'), ord('Q')):   stop_evt.set()

###############################################################################
# Entrypoint
###############################################################################
def _sig(*_): stop_evt.set()

def main():
    signal.signal(signal.SIGINT,  _sig)
    signal.signal(signal.SIGTERM, _sig)
    threading.Thread(target=_radio, daemon=True).start()
    curses.wrapper(_ui)
    stop_evt.set()
    json_fh.close(); db.close()

if __name__ == "__main__": main()
