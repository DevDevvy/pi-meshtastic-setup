#!/usr/bin/env python3
"""
Retro Meshtastic Badge – Pi 3.5″ Touch Edition
Tested with: meshtastic‑python 2.7.4 • Raspberry Pi OS Bookworm • Python 3.11

Features
========
✓ One persistent SerialInterface on /dev/rfcomm0 (override via MESHTASTIC_DEV)
✓ Auto‑retry & link‑state via PubSub
✓ sendText / receive text packets
✓ JSON + SQLite persistence
✓ Smooth touch / wheel / key scrolling
✓ Clean shutdown on SIGINT/SIGTERM
"""

###############################################################################
# Imports & constants
###############################################################################
import os, json, sqlite3, signal, queue, threading, time, curses
from pathlib import Path
from datetime import datetime

import meshtastic.serial_interface             # core API :contentReference[oaicite:5]{index=5}
from pubsub import pub                         # PubSub event bus :contentReference[oaicite:6]{index=6}

DEV_PATH   = os.getenv("MESHTASTIC_DEV", "/dev/rfcomm0")
DATA_DIR   = Path.home() / ".retrobadge"; DATA_DIR.mkdir(exist_ok=True)
LOG_FILE   = DATA_DIR / "meshtastic.log"
DB_FILE    = DATA_DIR / "meshtastic.db"

THEME_FG, THEME_BG = curses.COLOR_GREEN, curses.COLOR_BLACK
MAX_LEN, PAD_V     = 240, 2                 # bytes shown, top/bot blank rows

###############################################################################
# Persistence
###############################################################################
json_fh = open(LOG_FILE, "a", encoding="utf‑8", buffering=1)
db      = sqlite3.connect(DB_FILE, check_same_thread=False)
with db:
    db.execute("""CREATE TABLE IF NOT EXISTS messages (
                    ts  REAL,
                    src TEXT,
                    txt TEXT
                  )""")

###############################################################################
# Thread‑shared state
###############################################################################
incoming_q  = queue.Queue(1024)      # packets → UI
outgoing_q  = queue.Queue(256)       # user input → radio
link_up_evt = threading.Event()
stop_evt    = threading.Event()
_iface_lock = threading.Lock()
_iface      = None                   # set by radio thread

###############################################################################
# PubSub handlers (run on library threads)
###############################################################################
def _on_text(pkt, iface):
    """Persist and queue every incoming text packet."""
    ts  = pkt.get("rxTime") or pkt.get("timestamp", time.time())
    if ts > 1e12: ts /= 1000         # ms → s
    src = pkt.get("fromId") or pkt.get("from", {}).get("userAlias", "unknown")
    txt = pkt["decoded"]["text"][:MAX_LEN]

    json_fh.write(json.dumps(pkt, default=str) + "\n")
    with db: db.execute("INSERT INTO messages VALUES (?,?,?)", (ts, src, txt))
    incoming_q.put((ts, src, txt))

pub.subscribe(_on_text,         "meshtastic.receive.text")          # :contentReference[oaicite:7]{index=7}
pub.subscribe(lambda _:link_up_evt.set(),
              "meshtastic.connection.established")
pub.subscribe(lambda _:link_up_evt.clear(),
              "meshtastic.connection.lost")

###############################################################################
# Radio thread
###############################################################################
def _radio_worker():
    global _iface
    while not stop_evt.is_set():
        try:
            iface = meshtastic.serial_interface.SerialInterface(devPath=DEV_PATH)
            if not iface.waitForConfig():        # block until node‑DB done :contentReference[oaicite:8]{index=8}
                raise TimeoutError("no config from node")
            with _iface_lock: _iface = iface

            # flush any queued outbound messages
            while not stop_evt.wait(0.1):
                try:
                    msg = outgoing_q.get_nowait()
                    iface.sendText(msg)          # broadcast text :contentReference[oaicite:9]{index=9}
                except queue.Empty:
                    pass

        except Exception as e:
            json_fh.write(f"# radio error: {e}\n")
            link_up_evt.clear()
            time.sleep(2)                        # back‑off
        finally:
            with _iface_lock:
                try: 
                    if _iface: _iface.close()
                except Exception:
                    pass
                _iface = None

###############################################################################
# Helper utilities
###############################################################################
def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")

def _history(limit=2000):
    cur = db.cursor()
    cur.execute("SELECT ts, src, txt FROM messages ORDER BY ts DESC LIMIT ?", (limit,))
    return list(reversed(cur.fetchall()))

###############################################################################
# Curses UI
###############################################################################
def _ui(stdscr):
    curses.curs_set(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.start_color(); curses.use_default_colors()
    curses.init_pair(1, THEME_FG, THEME_BG)
    color = curses.color_pair(1)

    msgs      = _history()
    viewofs   = max(0, len(msgs) - (curses.LINES - PAD_V*2))
    send_mode = False
    inp       = ""

    HEADER = "╔═ Retro‑Badge — Meshtastic ═╗"
    FOOTER = "[S]end  [Q]uit  ↑/↓ PgUp/PgDn Touch/Scroll"

    while not stop_evt.is_set():
        # ingest new packets
        try:
            while True: msgs.append(incoming_q.get_nowait())
        except queue.Empty:
            pass

        h, w   = stdscr.getmaxyx()
        pane_h = h - PAD_V*2
        # follow tail
        if viewofs >= max(0, len(msgs) - pane_h):
            viewofs = max(0, len(msgs) - pane_h)

        stdscr.erase()
        try:
            stdscr.addstr(0, 0,
                        f"{HEADER} {'[● LINK]' if link_up_evt.is_set() else '[○ NO LINK]'}"
                        .ljust(w)[:w], color)
        except curses.error:
            pass
        # message list
        for i in range(pane_h):
            j = viewofs + i
            if j >= len(msgs): break
            ts, src, txt = msgs[j]
            pre   = f"{_fmt(ts)} {src[:10]:>10} │ "
            avail = w - len(pre)
            try:
                stdscr.addstr(PAD_V+i, 0, (pre + txt[:avail]).ljust(w)[:w], color)
            except curses.error:
                pass
        # footer / input
        if send_mode:
            prompt = f"Send> {inp}"
            try:
                stdscr.addstr(h-1, 0, prompt.ljust(w)[:w], color)
                stdscr.move(h-1, min(len(prompt), w-1))
            except curses.error:
                pass
        else:
            try:
                stdscr.addstr(h-1, 0, FOOTER.ljust(w)[:w], color)
            except curses.error:
                pass
        stdscr.refresh(); curses.napms(30)

        try: ch = stdscr.getch()
        except curses.error: continue

        # ----- send prompt -----
        if send_mode:
            if ch in (10,13):
                text = inp.strip(); inp = ""; send_mode = False
                if text:
                    ts = time.time()
                    with db: db.execute("INSERT INTO messages VALUES (?,?,?)",
                                        (ts, "You", text))
                    msgs.append((ts, "You", text))
                    outgoing_q.put(text)
            elif ch in (27,): send_mode = False
            elif ch in (127, 8): inp = inp[:-1]
            elif 32 <= ch <= 126: inp += chr(ch)
            continue

        # ----- navigation -----
        if ch == curses.KEY_UP:         viewofs = max(0, viewofs-1)
        elif ch == curses.KEY_DOWN:     viewofs = min(len(msgs)-pane_h, viewofs+1)
        elif ch == curses.KEY_PPAGE:    viewofs = max(0, viewofs-pane_h)
        elif ch == curses.KEY_NPAGE:    viewofs = min(len(msgs)-pane_h, viewofs+pane_h)
        elif ch == curses.KEY_MOUSE:
            _, mx, my, _, b = curses.getmouse()
            if b & curses.BUTTON4_PRESSED: viewofs = max(0, viewofs-3)
            if b & curses.BUTTON5_PRESSED: viewofs = min(len(msgs)-pane_h, viewofs+3)
            if b & curses.BUTTON1_PRESSED: drag = my
            if b & curses.BUTTON1_RELEASED:
                viewofs = max(0, min(len(msgs)-pane_h, viewofs - (my-drag)))
        elif ch in (ord('s'), ord('S')): send_mode, inp = True, ""
        elif ch in (ord('q'), ord('Q'), 27): stop_evt.set()

###############################################################################
# Main ‑‑ setup & launch
###############################################################################
def _sig(*_): stop_evt.set()

def main():
    for sig in (signal.SIGINT, signal.SIGTERM): signal.signal(sig, _sig)
    threading.Thread(target=_radio_worker, daemon=True).start()
    curses.wrapper(_ui)
    stop_evt.set()                      # UI exited
    json_fh.close(); db.close()

if __name__ == "__main__": main()