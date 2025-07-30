#!/usr/bin/env python3
import curses
import json
import sqlite3
import threading
from meshtastic.serial_interface import SerialInterface as BLEInterface

# --- CONFIG ---
LOG_JSON = True                   # append raw JSON to log file
LOG_SQLITE = True                 # insert messages into SQLite
LOG_FILE = "/home/pi/meshtastic.log"
DB_FILE  = "/home/pi/meshtastic.db"
DEV_PATH = "/dev/rfcomm0"
NODE_ADDR = "00:11:22:33:44:55"  # replace with your node's BLE address

# --- SETUP LOGGING ---
if LOG_JSON:
    json_fh = open(LOG_FILE, "a", encoding="utf-8")

if LOG_SQLITE:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("""
      CREATE TABLE IF NOT EXISTS messages (
        ts   REAL,
        src  TEXT,
        text TEXT
      )
    """)
    conn.commit()

# --- MESHTASTIC CALLBACK ---
messages = []
lock      = threading.Lock()
iface     = None       # will hold the SerialInterface instance
def on_receive(packet=None, interface=None, **kwargs):
    if not isinstance(packet, dict):
        return

    decoded = packet.get("decoded", {})
    text = decoded.get("text")               # same key Meshtastic uses for plain messages
    if not text:
        return                               # ignore position pings etc.

    # 'from' is usually an int node‑id; fall back to 'unknown' if missing
    src_id = packet.get("from", "unknown")
    src = str(src_id)

    ts = packet.get("timestamp", 0)
    if ts > 1e11: ts /= 1000  # convert ms to seconds if needed
    with lock:
        messages.append((src, text))

    if LOG_JSON:
        json_fh.write(json.dumps(packet) + "\n")

    if LOG_SQLITE:
        conn.execute(
            "INSERT INTO messages VALUES (?, ?, ?)",
            (ts, src, text)
        )
        conn.commit()
            
def serial_listener():
    global iface
    iface = BLEInterface(devPath=DEV_PATH)
    iface.onReceive = on_receive
    iface.start()   # this spawns its own RX thread
    iface.waitUntilDisconnected()     # keep this thread alive
# --- UI ---
def run_ui(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)

    # Colors: green on black for retro feel
    curses.start_color()
    curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)

    threading.Thread(target=serial_listener, daemon=True).start()

    offset = 0
    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()

        # Header & Footer
        stdscr.attron(curses.color_pair(1))
        stdscr.addstr(0, 0, "╔" + ("═"*(w-2)) + "╗")
        stdscr.addstr(1, 0, "║ RetroMeshtastic Badge — Touch or ↑/↓ to scroll ║".ljust(w-1) + "║")
        stdscr.addstr(h-2, 0, "╚" + ("═"*(w-2)) + "╝")
        stdscr.addstr(h-1, 0, "Press 's' to send | Ctrl-C to exit".ljust(w-1))
        stdscr.attroff(curses.color_pair(1))

        # Message window inside box
        with lock:
            offset = min(offset, max(len(messages) - (h-4), 0))
            visible = messages[offset : offset + max(h-4, 0)]
        for i, (src, txt) in enumerate(visible):
            line = f"{src[:10]}: {txt}"
            stdscr.addstr(2+i, 1, line[:w-2], curses.color_pair(1))

        stdscr.refresh()
        curses.napms(50)

        # Input handling
        try:
            ch = stdscr.getch()
        except curses.error:
            continue

        if ch == curses.KEY_UP:
            offset = max(0, offset-1)
        elif ch == curses.KEY_DOWN:
            with lock:
                bottom = max(0, len(messages) - (h-4))
            offset = min(bottom, offset + 1)
        elif ch in (ord('s'), ord('S')):
            if iface:  # only if the radio’s ready
                # 1) Show the cursor and let getstr() block
                curses.curs_set(1)
                stdscr.nodelay(False)

                # 2) Echo your typing and draw the prompt
                curses.echo()
                stdscr.addstr(h-1, 0, "Send: ".ljust(w-1), curses.color_pair(1))
                stdscr.move(h-1, 6)
                stdscr.refresh()

                # 3) Actually read the line (blocks until Enter)
                try:
                    txt = stdscr.getstr(h-1, 6, w-8).decode()
                except Exception:
                    txt = ""
                finally:
                    # 4) Turn echo & cursor back off, restore non‑blocking
                    curses.noecho()
                    curses.curs_set(0)
                    stdscr.nodelay(True)

                # 5) If you typed something, send it
                if txt:
                    iface.sendText(txt)
                    with lock:
                        messages.append(("You", txt))


if __name__ == "__main__":
    curses.wrapper(run_ui)