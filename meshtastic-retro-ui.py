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
LOG_JSON   = True
LOG_SQLITE = True
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
        print(f"Warning: log file open failed: {e}", file=sys.stderr)

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
        print(f"Warning: sqlite setup failed: {e}", file=sys.stderr)

# --- GLOBALS ---
messages          = []
lock              = threading.Lock()
iface             = None
interface_ready   = threading.Event()
connection_status = "Idle"

# --- CALLBACKS ---
def on_receive(packet, topic=pub.AUTO_TOPIC):
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
                    "ts":        ts,
                    "from":      src,
                    "text":      text,
                    "raw_packet": str(packet)
                }) + "\n")
                json_fh.flush()
            if LOG_SQLITE and conn:
                conn.execute("INSERT INTO messages VALUES (?, ?, ?)", (ts, src, text))
                conn.commit()
    except Exception as e:
        with lock:
            messages.append(("ERROR", f"on_receive error: {e}"))

def on_connection(interface, topic=pub.AUTO_TOPIC):
    global connection_status
    connection_status = "Connected"
    interface_ready.set()

def on_lost_connection(interface, topic=pub.AUTO_TOPIC):
    global connection_status
    connection_status = "Disconnected"
    interface_ready.clear()

# --- INITIALIZE MESHTASTIC IN BG THREAD ---
def init_interface():
    global iface, connection_status
    # subscribe to events
    pub.subscribe(on_receive,         "meshtastic.receive")
    pub.subscribe(on_connection,      "meshtastic.connection.established")
    pub.subscribe(on_lost_connection, "meshtastic.connection.lost")

    connection_status = f"Opening {DEV_PATH}…"
    try:
        iface = SerialInterface(devPath=DEV_PATH, connectNow=True)
    except Exception as e:
        connection_status = f"Open error: {e}"
        return

    # after connectNow, let the callbacks drive status

# --- UI & SENDING ---
def send_message(text):
    if not iface or not interface_ready.is_set():
        return False, "Not ready"
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

    # Kick off Meshtastic connect in background
    threading.Thread(target=init_interface, daemon=True).start()
    connection_status = "Connecting…"

    offset       = 0
    status_msg   = ""
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
            total    = len(messages)
            max_off  = max(0, total - (h-5))
            offset   = min(offset, max_off)
            view     = messages[offset : offset + (h-5)]
        for i, (src, txt) in enumerate(view):
            clr = curses.color_pair(3) if src == "You" else curses.color_pair(1)
            if src in ("ERROR", "SYSTEM"):
                clr = curses.color_pair(2)
            line = f"{src[:12]}: {txt}"
            stdscr.addstr(2+i, 1, line[:w-2], clr)

        # Status line + Footer
        if status_msg:
            stdscr.addstr(h-3, 1, status_msg[:w-2], curses.color_pair(status_color))
        stdscr.attron(curses.color_pair(1))
        stdscr.addstr(h-2, 0, "╚" + ("═"*(w-2)) + "╝")
        stdscr.addstr(h-1, 0, "Press 's' to send | ↑/↓ scroll | Ctrl-C exit".ljust(w-1))
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
                status_msg, status_color = "Interface not ready", 2
                continue
            curses.echo(); curses.curs_set(1); stdscr.timeout(-1)
            stdscr.addstr(h-1, 0, "Send: ".ljust(w-1)); stdscr.refresh()
            txt = stdscr.getstr(h-1, 6, w-8).decode().strip()
            curses.noecho(); curses.curs_set(0); stdscr.timeout(100)
            if txt:
                ok, res = send_message(txt)
                status_msg, status_color = (res, 1) if ok else (res, 2)
            else:
                status_msg, status_color = ("Empty – not sent", 3)
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
