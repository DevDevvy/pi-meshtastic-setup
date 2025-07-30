#!/usr/bin/env python3
"""
Retro Hacker-Style Meshtastic Terminal UI for Raspberry Pi (Terminal Only)
Fix: Corrected callback signatures so connection event fires and UI no longer hangs at "Connecting".
"""
import locale
locale.setlocale(locale.LC_ALL, '')

import curses
import json
import sqlite3
import threading
import time
import os
import stat
from pathlib import Path
from meshtastic.serial_interface import SerialInterface
from pubsub import pub  # pip install pypubsub

# --- CONFIGURATION ---
HOME = Path.home()
LOG_JSON = True
LOG_SQLITE = True
LOG_FILE = HOME / "meshtastic.log"
DB_FILE = HOME / "meshtastic.db"
DEV_PATH = "/dev/rfcomm0"

# --- GLOBALS ---
messages = []            # list of (src, text)
lock = threading.Lock()
iface = None            # SerialInterface
interface_ready = threading.Event()
connection_status = "Starting…"
json_fh = None
db_conn = None

# --- DATABASE SETUP ---

def setup_db():
    conn = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            src TEXT,
            text TEXT
        )
        """
    )
    conn.commit()
    return conn

# --- CALLBACKS (Correct signatures!) ---

def on_receive(packet, interface, topic=pub.AUTO_TOPIC):
    global db_conn, json_fh
    d = getattr(packet, "decoded", None)
    if d and getattr(d, "text", None):
        txt = d.text
        src = str(getattr(packet, "fromId", getattr(packet, "from_peer", "unknown")))
        ts = getattr(packet, "rxTime", time.time())
        with lock:
            messages.append((src, txt))
        if LOG_JSON and json_fh:
            try:
                json_fh.write(json.dumps({"ts": ts, "src": src, "text": txt}) + "\n")
                json_fh.flush()
            except:
                pass
        if LOG_SQLITE and db_conn:
            try:
                db_conn.execute(
                    "INSERT INTO messages (ts, src, text) VALUES (?, ?, ?)",
                    (ts, src, txt)
                )
                db_conn.commit()
            except:
                pass


def on_connection(interface, topic=pub.AUTO_TOPIC):
    global connection_status
    connection_status = "Connected"
    interface_ready.set()
    with lock:
        messages.append(("SYSTEM", "Connection established"))


def on_lost_connection(interface, topic=pub.AUTO_TOPIC):
    global connection_status
    connection_status = "Disconnected"
    interface_ready.clear()
    with lock:
        messages.append(("SYSTEM", "Connection lost"))

# --- DEVICE CHECK ---


def check_device():
    if not os.path.exists(DEV_PATH):
        return False
    return stat.S_ISCHR(os.stat(DEV_PATH).st_mode)

# --- SERIAL LISTENER THREAD ---

def serial_listener():
    global iface, connection_status
    if not check_device():
        connection_status = f"Device not found: {DEV_PATH}"
        with lock:
            messages.append(("SYSTEM", connection_status))
        return

    connection_status = f"Connecting to {DEV_PATH}"
    with lock:
        messages.append(("SYSTEM", connection_status))

    pub.subscribe(on_receive, "meshtastic.receive")
    pub.subscribe(on_connection, "meshtastic.connection.established")
    pub.subscribe(on_lost_connection, "meshtastic.connection.lost")

    try:
        iface = SerialInterface(devPath=DEV_PATH)
    except Exception as e:
        connection_status = f"Open error: {e}"
        with lock:
            messages.append(("SYSTEM", connection_status))
        return

    # Wait up to 15 seconds for the connection to become ready
    if not iface.isConnected.wait(15):
        connection_status = "Connection timeout"
        with lock:
            messages.append(("SYSTEM", "Unable to connect"))
        return
    # isConnected event might fire before our subscriber is ready
    if not interface_ready.is_set():
        on_connection(iface)

    while interface_ready.is_set():
        time.sleep(1)

# --- MESSAGE SENDER ---


def send_message(txt):
    if not iface or not interface_ready.is_set():
        return False, "Not ready"
    try:
        iface.sendText(txt)
        with lock:
            messages.append(("You", txt))
        return True, "Sent"
    except Exception as e:
        return False, f"Error: {e}"

# --- CURSES UI LOOP ---

def run_ui(stdscr):
    global json_fh, db_conn
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    stdscr.keypad(True)

    if LOG_JSON:
        try:
            json_fh = open(LOG_FILE, "a", encoding="utf-8")
        except:
            pass
    if LOG_SQLITE:
        db_conn = setup_db()

    threading.Thread(target=serial_listener, daemon=True).start()

    offset = 0
    status_msg = ""
    status_color = 1

    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()

        # Header
        stdscr.attron(curses.color_pair(1))
        stdscr.addstr(0, 0, "╔" + "═"*(w-2) + "╗")
        header = f" RetroMeshtastic — {connection_status}"
        stdscr.addstr(1, 0, "║" + header.ljust(w-2) + "║")
        stdscr.addstr(2, 0, "╠" + "═"*(w-2) + "╣")
        stdscr.attroff(curses.color_pair(1))

        with lock:
            total = len(messages)
            max_off = max(0, total - (h - 6))
            offset = min(offset, max_off)
            view = messages[offset:offset + (h - 6)]
        for i, (src, txt) in enumerate(view):
            clr = curses.color_pair(3) if src == "You" else curses.color_pair(1)
            if src in ("SYSTEM", "ERROR"):
                clr = curses.color_pair(2)
            stdscr.addstr(3 + i, 1, f"{src[:12]}: {txt}"[:w-2], clr)

        if status_msg:
            stdscr.addstr(h-3, 1, status_msg[:w-2], curses.color_pair(status_color))

        stdscr.attron(curses.color_pair(1))
        stdscr.addstr(h-2, 0, "╚" + "═"*(w-2) + "╝")
        stdscr.attroff(curses.color_pair(1))
        stdscr.addstr(h-1, 0, "Press s/send | ↑/↓ scroll | Ctrl-C exit".ljust(w-1))
        stdscr.refresh()

        ch = stdscr.getch()
        if ch == curses.KEY_UP:
            offset = max(0, offset - 1)
        elif ch == curses.KEY_DOWN:
            offset = min(max_off, offset + 1)
        elif ch in (ord('s'), ord('S')):
            if not interface_ready.is_set():
                status_msg, status_color = "Not connected", 2
                continue
            curses.echo(); curses.curs_set(1)
            stdscr.addstr(h-1, 0, "Send: ".ljust(w-1)); stdscr.refresh()
            try:
                inp = stdscr.getstr(h-1, 6, w-7).decode().strip()
                if inp:
                    ok, res = send_message(inp)
                    status_msg, status_color = res, 1 if ok else 2
                else:
                    status_msg, status_color = "Empty message", 3
            except Exception:
                status_msg, status_color = "Input error", 2
            curses.noecho(); curses.curs_set(0)
        elif ch == 3:  # Ctrl+C
            break
        time.sleep(0.1)

# --- CLEANUP ---

def cleanup():
    global iface, json_fh, db_conn
    if iface:
        try:
            iface.close()
        except:
            pass
    if json_fh:
        try:
            json_fh.close()
        except:
            pass
    if db_conn:
        try:
            db_conn.close()
        except:
            pass

if __name__ == "__main__":
    try:
        curses.wrapper(run_ui)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
        print("RetroMeshtastic closed.")
