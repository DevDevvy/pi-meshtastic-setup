#!/usr/bin/env python3
import curses
import json
import sqlite3
import threading
import time
import sys
from pathlib import Path

from meshtastic.serial_interface import SerialInterface
from pubsub import pub           # pip install pypubsub

# --- CONFIG ---
LOG_JSON   = True                # append raw JSON to log file
LOG_SQLITE = True                # insert messages into SQLite
HOME       = Path.home()
LOG_FILE   = HOME / "meshtastic.log"
DB_FILE    = HOME / "meshtastic.db"
DEV_PATH   = "/dev/rfcomm0"

# --- SETUP LOGGING ---
json_fh = None
conn    = None

if LOG_JSON:
    try:
        json_fh = open(LOG_FILE, "a", encoding="utf-8")
    except Exception as e:
        print(f"Warning: Could not open log file: {e}", file=sys.stderr)

if LOG_SQLITE:
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        conn.execute("""
          CREATE TABLE IF NOT EXISTS messages (
            ts   REAL,
            src  TEXT,
            text TEXT
          )
        """)
        conn.commit()
    except Exception as e:
        print(f"Warning: Could not setup SQLite: {e}", file=sys.stderr)

# --- GLOBAL STATE ---
messages         = []
lock             = threading.Lock()
iface            = None
interface_ready  = threading.Event()
connection_status = "Initializing…"

# --- CALLBACKS ---
def on_receive(packet, topic=pub.AUTO_TOPIC):
    """Called for every Meshtastic packet."""
    global messages, json_fh, conn
    try:
        decoded = getattr(packet, "decoded", None)
        if decoded and getattr(decoded, "text", None):
            text = decoded.text
            src  = str(getattr(packet, "fromId", getattr(packet, "from", "unknown")))
            ts   = getattr(packet, "rxTime", time.time())
            with lock:
                messages.append((src, text))
            if LOG_JSON and json_fh:
                json_fh.write(json.dumps({
                    "timestamp": ts,
                    "from":      src,
                    "text":      text,
                    "raw":       str(packet)
                }) + "\n")
                json_fh.flush()
            if LOG_SQLITE and conn:
                conn.execute("INSERT INTO messages VALUES (?, ?, ?)", (ts, src, text))
                conn.commit()
    except Exception as e:
        with lock:
            messages.append(("ERROR", f"on_receive error: {e}"))

def on_connection(interface, topic=pub.AUTO_TOPIC):
    """Fired when Meshtastic link is up."""
    global connection_status
    connection_status = "Connected"
    interface_ready.set()

def on_lost_connection(interface, topic=pub.AUTO_TOPIC):
    """Fired when Meshtastic link is lost."""
    global connection_status
    connection_status = "Disconnected"
    interface_ready.clear()

# --- INIT INTERFACE ---
def init_interface():
    """Subscribe to topics and open the RFCOMM link."""
    global iface, connection_status
    # 1) Subscribe callbacks
    pub.subscribe(on_receive,             "meshtastic.receive")
    pub.subscribe(on_connection,          "meshtastic.connection.established")
    pub.subscribe(on_lost_connection,     "meshtastic.connection.lost")
    # 2) Open link (spawns its own I/O thread)
    connection_status = f"Opening {DEV_PATH}…"
    iface = SerialInterface(devPath=DEV_PATH, connectNow=True)

# --- UI & MAIN LOOP ---
def send_message(text):
    """Helper to send a message."""
    if not iface or not interface_ready.is_set():
        return False, "Interface not ready"
    try:
        iface.sendText(text)
        with lock:
            messages.append(("You", text))
        return True, "Sent"
    except Exception as e:
        return False, f"Send error: {e}"

def run_ui(stdscr):
    global connection_status
    # Curses setup
    curses.curs_set(0)
    stdscr.keypad(True)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.start_color()
    curses.init_pair(1, curses.COLOR_GREEN,  curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_RED,    curses.COLOR_BLACK)
    curses.init_pair(3, curses.COLOR_YELLOW, curses.COLOR_BLACK)

    # Kick off Meshtastic
    init_interface()
    connection_status = "Connecting…"
    if not interface_ready.wait(timeout=10):
        connection_status = "Timeout – check RFCOMM"
    else:
        connection_status = "Connected"

    offset = 0
    status_msg  = ""
    status_color = 1

    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()

        # Header
        stdscr.attron(curses.color_pair(1))
        stdscr.addstr(0, 0, "╔" + ("═"*(w-2)) + "╗")
        header = f"║ RetroMeshtastic — {connection_status}"
        stdscr.addstr(1, 0, header.ljust(w-1) + "║")
        stdscr.attroff(curses.color_pair(1))

        # Messages
        with lock:
            total = len(messages)
            max_off = max(0, total - (h-5))
            offset = min(offset, max_off)
            view = messages[offset : offset + (h-5)]
        for i, (src, txt) in enumerate(view):
            color = curses.color_pair(3) if src == "You" else curses.color_pair(1)
            if src in ("ERROR", "SYSTEM"):
                color = curses.color_pair(2)
            line = f"{src[:12]}: {txt}"
            stdscr.addstr(2+i, 1, line[:w-2], color)

        # Footer
        if status_msg:
            stdscr.addstr(h-3, 1, status_msg[:w-2], curses.color_pair(status_color))
        stdscr.attron(curses.color_pair(1))
        stdscr.addstr(h-2, 0, "╚" + ("═"*(w-2)) + "╝")
        stdscr.addstr(h-1, 0, "Press 's' to send | ↑/↓ scroll | Ctrl-C to exit".ljust(w-1))
        stdscr.attroff(curses.color_pair(1))

        stdscr.refresh()
        stdscr.timeout(100)
        try:
            ch = stdscr.getch()
        except curses.error:
            continue

        if ch == curses.KEY_UP:
            offset = max(0, offset-1)
        elif ch == curses.KEY_DOWN:
            offset = min(max_off, offset+1)
        elif ch in (ord('s'), ord('S')):
            status_msg = ""
            if not interface_ready.is_set():
                status_msg, status_color = "Not ready", 2
                continue
            # get user input
            stdscr.timeout(-1)
            curses.echo()
            curses.curs_set(1)
            stdscr.addstr(h-1, 0, "Send: ".ljust(w-1)); stdscr.refresh()
            txt = stdscr.getstr(h-1, 6, w-8).decode().strip()
            curses.noecho(); curses.curs_set(0); stdscr.timeout(100)
            if txt:
                ok, res = send_message(txt)
                status_msg, status_color = ("Sent", 1) if ok else (res, 2)
            else:
                status_msg, status_color = ("Empty, not sent", 3)
        elif ch == 3:  # Ctrl-C
            break

def cleanup():
    if iface:
        try: iface.close()
        except: pass
    if json_fh:
        try: json_fh.close()
        except: pass
    if conn:
        try: conn.close()
        except: pass

if __name__ == "__main__":
    try:
        curses.wrapper(run_ui)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
        print("Meshtastic interface closed.")
