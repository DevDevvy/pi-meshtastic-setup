#!/usr/bin/env python3
import locale
locale.setlocale(locale.LC_ALL, '')

import curses
import json
import sqlite3
import threading
import time
import sys
import os
import stat
from pathlib import Path

from meshtastic.serial_interface import SerialInterface
from pubsub import pub   # pip install pypubsub

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

# --- GLOBALS ---
messages          = []
lock              = threading.Lock()
iface             = None
interface_ready   = threading.Event()
connection_status = "Starting…"

# --- CALLBACKS ---
def on_receive(packet, topic=None):
    try:
        d = getattr(packet, "decoded", None)
        if d and getattr(d, "text", None):
            txt = d.text
            src = str(getattr(packet, "fromId", getattr(packet, "from", "unknown")))
            ts  = getattr(packet, "rxTime", time.time())
            with lock:
                messages.append((src, txt))
            if LOG_JSON and json_fh:
                json_fh.write(json.dumps({
                    "ts": ts, "from": src, "text": txt, "raw": str(packet)
                }) + "\n")
                json_fh.flush()
            if LOG_SQLITE and conn:
                conn.execute("INSERT INTO messages VALUES (?,?,?)", (ts, src, txt))
                conn.commit()
    except Exception as e:
        with lock:
            messages.append(("ERROR", f"on_receive error: {e}"))

def on_connection(topic=None):
    global connection_status
    connection_status = "Connected"
    interface_ready.set()
    with lock:
        messages.append(("SYSTEM", "meshtastic.connection.established"))

def on_lost_connection(topic=None):
    global connection_status
    connection_status = "Disconnected"
    interface_ready.clear()
    with lock:
        messages.append(("SYSTEM", "meshtastic.connection.lost"))

# --- UTIL ---
def check_device():
    if not os.path.exists(DEV_PATH):
        return False, f"{DEV_PATH} not found"
    st = os.stat(DEV_PATH)
    if not stat.S_ISCHR(st.st_mode):
        return False, f"{DEV_PATH} not a char device"
    return True, "OK"

# --- SERIAL THREAD ---
def serial_listener():
    global iface, connection_status

    ok, msg = check_device()
    if not ok:
        connection_status = "Device error"
        with lock:
            messages.extend([
                ("SYSTEM", f"Device check failed: {msg}"),
                ("SYSTEM", "Run sudo rfcomm bind or pair via bluetoothctl"),
            ])
        return

    # let UI know we're starting
    connection_status = f"Connecting to {DEV_PATH}"
    with lock:
        messages.append(("SYSTEM", connection_status))

    # subscribe *before* opening
    pub.subscribe(on_receive,           "meshtastic.receive")
    pub.subscribe(on_connection,        "meshtastic.connection.established")
    pub.subscribe(on_lost_connection,   "meshtastic.connection.lost")

    try:
        iface = SerialInterface(devPath=DEV_PATH)
    except Exception as e:
        connection_status = f"Open error: {e}"
        with lock:
            messages.append(("SYSTEM", connection_status))
        return

    # wait up to 10 s for on_connection() to fire
    connection_status = "Waiting for connection…"
    with lock:
        messages.append(("SYSTEM", connection_status))
    if not interface_ready.wait(10):
        connection_status = "Connection failed"
        with lock:
            messages.append(("SYSTEM", "Unable to connect after timeout"))
        return

    # now we’re connected
    # (on_connection has already set both interface_ready & connection_status)
    try:
        info = iface.getMyNodeInfo()
        with lock:
            messages.append(("SYSTEM", f"Node info: {str(info)[:60]}…"))
    except:
        pass

    # keep‑alive
    while True:
        time.sleep(1)
        if not interface_ready.is_set():
            break

# --- SENDER ---
def send_message(txt):
    if not iface or not interface_ready.is_set():
        return False, "Interface not ready"
    try:
        iface.sendText(txt)
        with lock:
            messages.append(("You", txt))
        return True, "Sent"
    except Exception as e:
        return False, f"Send error: {e}"

# --- UI LOOP ---
def run_ui(stdscr):
    curses.curs_set(0)
    stdscr.keypad(True)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.start_color()
    curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_RED,   curses.COLOR_BLACK)
    curses.init_pair(3, curses.COLOR_YELLOW,curses.COLOR_BLACK)

    # kick off serial thread
    threading.Thread(target=serial_listener, daemon=True).start()

    offset = 0
    status_msg, status_color = "", 1

    while True:
        h,w = stdscr.getmaxyx()
        stdscr.erase()

        # Header
        stdscr.attron(curses.color_pair(1))
        stdscr.addstr(0,0, "╔" + "═"*(w-2) + "╗")
        stdscr.addstr(1,0, f"║ RetroMeshtastic — {connection_status}".ljust(w-1)+"║")
        stdscr.attroff(curses.color_pair(1))

        # Messages
        with lock:
            total = len(messages)
            max_off = max(0, total-(h-5))
            offset = min(offset, max_off)
            view = messages[offset:offset+(h-5)]
        for i,(src,txt) in enumerate(view):
            clr = curses.color_pair(3) if src=="You" else curses.color_pair(1)
            if src in ("ERROR","SYSTEM"):
                clr = curses.color_pair(2)
            stdscr.addstr(2+i,1, f"{src[:12]}: {txt}"[:w-2], clr)

        # Status & Footer
        if status_msg:
            stdscr.addstr(h-3,1, status_msg[:w-2], curses.color_pair(status_color))
        stdscr.attron(curses.color_pair(1))
        stdscr.addstr(h-2,0, "╚" + "═"*(w-2) + "╝")
        stdscr.addstr(h-1,0, "Press s to send | ↑/↓ scroll | Ctrl-C exit".ljust(w-1))
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
        elif ch in (ord('s'),ord('S')):
            status_msg = ""
            if not interface_ready.is_set():
                status_msg, status_color = "Interface not ready", 2
                continue
            stdscr.timeout(-1); curses.echo(); curses.curs_set(1)
            stdscr.addstr(h-1,0, "Send: ".ljust(w-1)); stdscr.refresh()
            try:
                inp = stdscr.getstr(h-1,6,w-8).decode().strip()
                if inp:
                    ok,res = send_message(inp)
                    status_msg, status_color = (res,1) if ok else (res,2)
                else:
                    status_msg, status_color = ("Empty – not sent",3)
            except Exception as e:
                status_msg, status_color = (f"Input error: {e}",2)
            finally:
                curses.noecho(); curses.curs_set(0)
                stdscr.timeout(100)
        elif ch == 3:  # Ctrl‑C
            break

# --- CLEANUP ---
def cleanup():
    global iface, json_fh, conn
    if iface:
        try: iface.close()
        except: pass
    if json_fh:
        try: json_fh.close()
        except: pass
    if conn:
        try: conn.close()
        except: pass

if __name__=="__main__":
    try:
        curses.wrapper(run_ui)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
        print("Meshtastic interface closed.")
