#!/usr/bin/env python3
import curses
import json
import sqlite3
from meshtastic.ble_interface import BLEInterface
from pubsub import pub
import threading

# --- CONFIG ---
LOG_JSON = True                   # append raw JSON to log file
LOG_SQLITE = True                 # insert messages into SQLite
LOG_FILE = "/home/rangerdan/meshtastic.log"
DB_FILE  = "/home/rangerdan/meshtastic.db"
DEV_PATH = "/dev/rfcomm0"
NODE_ADDR = "48:CA:43:3C:51:FD"  # replace with your node's BLE address

# --- SETUP LOGGING ---
if LOG_JSON:
    json_fh = open(LOG_FILE, "a", encoding="utf-8")

if LOG_SQLITE:
    conn = sqlite3.connect(DB_FILE)
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
def on_receive(packet, interface):
    js = packet.get("decoded", {})
    text = js.get("text")
    src  = packet.get("from", {}).get("userAlias", "unknown")
    if text:
        ts = packet.get("timestamp", 0)/1000
        messages.append((src, text))
        if LOG_JSON:
            json_fh.write(json.dumps(packet) + "\n")
        if LOG_SQLITE:
            conn.execute(
              "INSERT INTO messages VALUES (?, ?, ?)",
              (ts, src, text)
            )
            conn.commit()

# --- UI ---
def run_ui(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)

    # Colors: green on black for retro feel
    curses.start_color()
    curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)

    iface = BLEInterface(address=NODE_ADDR)
    pub.subscribe(on_receive, "meshtastic.receive")
    threading.Thread(target=iface.loop_forever, daemon=True).start()

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
        for i in range(min(len(messages)-offset, h-4)):
            src, txt = messages[offset+i]
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
            offset = min(max(0, len(messages)-(h-4)), offset+1)
        elif ch == curses.KEY_MOUSE:
            _, mx, my, _, _ = curses.getmouse()
            if my < h//2:
                offset = max(0, offset-1)
            else:
                offset = min(max(0, len(messages)-(h-4)), offset+1)
        elif ch in (ord('s'), ord('S')):
            curses.echo()
            stdscr.addstr(h-1, 0, "Send: ".ljust(w-1))
            txt = stdscr.getstr(h-1, 6, w-8).decode()
            curses.noecho()
            if txt:
                iface.sendText(txt)
                messages.append(("You", txt))

if __name__ == "__main__":
    curses.wrapper(run_ui)