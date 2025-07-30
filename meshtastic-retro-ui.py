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
connection_status = "Connecting…"

# --- CALLBACKS ---
def on_receive(packet, topic=None):
    """Callback for received packets"""
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
                    "raw_packet": str(packet)
                }) + "\n")
                json_fh.flush()

            if LOG_SQLITE and conn:
                conn.execute("INSERT INTO messages VALUES (?, ?, ?)", (ts, src, text))
                conn.commit()

    except Exception as e:
        with lock:
            messages.append(("ERROR", f"Packet processing error: {e}"))

def on_connection(topic=None):
    global connection_status
    connection_status = "Connected"
    interface_ready.set()

def on_lost_connection(topic=None):
    global connection_status
    connection_status = "Disconnected"
    interface_ready.clear()

def check_device_exists():
    """Ensure /dev/rfcomm0 is present and correct type"""
    if not os.path.exists(DEV_PATH):
        return False, f"{DEV_PATH} does not exist"
    try:
        st = os.stat(DEV_PATH)
        if not stat.S_ISCHR(st.st_mode):
            return False, f"{DEV_PATH} is not a character device"
        return True, "OK"
    except Exception as e:
        return False, str(e)

def serial_listener():
    """Initialize Meshtastic interface in background thread"""
    global iface, connection_status

    # 1) Device sanity check
    ok, msg = check_device_exists()
    if not ok:
        connection_status = f"Device Error: {msg}"
        with lock:
            messages.extend([
                ("SYSTEM", f"Device check failed: {msg}"),
                ("SYSTEM", "Run 'sudo rfcomm bind' or pair with bluetoothctl"),
            ])
        return

    # 2) Try opening
    try:
        connection_status = f"Connecting to {DEV_PATH}"
        with lock:
            messages.append(("SYSTEM", f"Attempting to connect to {DEV_PATH}"))

        iface = SerialInterface(devPath=DEV_PATH)
        iface.onReceive       = on_receive
        iface.onConnection    = on_connection
        iface.onLostConnection= on_lost_connection

        # 3) Wait up to 10s for link-up
        for i in range(20):
            time.sleep(0.5)
            connection_status = f"Waiting... ({i+1}/20)"
            if getattr(iface, "isConnected", False) or getattr(iface.stream, "is_open", False):
                break

        if not (iface.isConnected or iface.stream.is_open):
            connection_status = "Connection failed"
            with lock:
                messages.extend([
                    ("SYSTEM", "Unable to connect after timeout"),
                ])
            return

        # 4) Success
        connection_status = "Connected"
        interface_ready.set()
        with lock:
            messages.append(("SYSTEM", "Successfully connected"))

        # 5) Node info (optional)
        try:
            info = iface.getMyNodeInfo()
            with lock:
                messages.append(("SYSTEM", f"Node info: {str(info)[:60]}…"))
        except:
            pass

        # 6) Keep‐alive loop
        while True:
            time.sleep(1)
            if not (iface.isConnected or iface.stream.is_open):
                connection_status = "Disconnected"
                interface_ready.clear()
                with lock:
                    messages.append(("SYSTEM", "Lost connection"))
                break

    except Exception as e:
        connection_status = f"Fatal Error: {e}"
        with lock:
            messages.append(("SYSTEM", f"Fatal connection error: {e}"))

def send_message(text):
    """Send text if interface is ready"""
    if not iface or not interface_ready.is_set():
        return False, "Interface not ready"
    try:
        iface.sendText(text)
        with lock:
            messages.append(("You", text))
        return True, "Message sent"
    except Exception as e:
        return False, f"Send error: {e}"

# --- UI LOOP ---
def run_ui(stdscr):
    curses.curs_set(0)
    stdscr.keypad(True)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.start_color()
    curses.init_pair(1, curses.COLOR_GREEN,  curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_RED,    curses.COLOR_BLACK)
    curses.init_pair(3, curses.COLOR_YELLOW, curses.COLOR_BLACK)

    # start serial in background
    threading.Thread(target=serial_listener, daemon=True).start()

    offset = 0
    status_msg   = ""
    status_color = 1

    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()

        # ┌── HEADER ──┐
        stdscr.attron(curses.color_pair(1))
        stdscr.addstr(0, 0, "╔" + "═"*(w-2) + "╗")
        header = f"║ RetroMeshtastic — {connection_status}"
        stdscr.addstr(1, 0, header.ljust(w-1) + "║")
        stdscr.attroff(curses.color_pair(1))

        # ■ Messages window
        with lock:
            total    = len(messages)
            max_off  = max(0, total - (h-5))
            offset   = min(offset, max_off)
            view     = messages[offset:offset+(h-5)]

        for i, (src, txt) in enumerate(view):
            clr = curses.color_pair(3) if src == "You" else curses.color_pair(1)
            if src in ("ERROR","SYSTEM"):
                clr = curses.color_pair(2)
            line = f"{src[:12]}: {txt}"
            stdscr.addstr(2+i, 1, line[:w-2], clr)

        # Status line
        if status_msg:
            stdscr.addstr(h-3, 1, status_msg[:w-2], curses.color_pair(status_color))

        # ╰── FOOTER ──╯
        stdscr.attron(curses.color_pair(1))
        stdscr.addstr(h-2, 0, "╚" + "═"*(w-2) + "╝")
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

            # blocking input
            stdscr.timeout(-1)
            curses.echo(); curses.curs_set(1)
            stdscr.addstr(h-1, 0, "Send: ".ljust(w-1))
            stdscr.refresh()

            try:
                inp = stdscr.getstr(h-1, 6, w-8).decode().strip()
                if inp:
                    ok, res = send_message(inp)
                    status_msg, status_color = (res, 1) if ok else (res, 2)
                else:
                    status_msg, status_color = ("Empty – not sent", 3)
            except Exception as e:
                status_msg, status_color = (f"Input error: {e}", 2)
            finally:
                curses.noecho(); curses.curs_set(0)
                stdscr.timeout(100)

        elif ch == 3:  # Ctrl‑C
            break

def cleanup():
    """Tidy up on exit"""
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

if __name__ == "__main__":
    try:
        curses.wrapper(run_ui)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
        print("Meshtastic interface closed.")
