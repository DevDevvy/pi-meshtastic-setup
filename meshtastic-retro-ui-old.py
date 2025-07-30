#!/usr/bin/env python3
"""
Retro Hacker-Style Meshtastic Terminal UI for Raspberry Pi
- Connects via Bluetooth to Meshtastic node at /dev/rfcomm0
- Terminal-only (curses) interface on a 3.5" touchscreen
- Scroll through received messages
- Press 's' to send a message
- Press Ctrl+C to exit
- Saves all messages to local SQLite database for later analysis
"""
import curses
import sqlite3
import threading
import time
import traceback
from meshtastic.serial_interface import SerialInterface
from meshtastic.packet_pb2 import Packet  # if needed for type hints

# --- CONFIGURATION ---
DEV_PATH = "/dev/rfcomm0"
DB_FILE = "/home/pi/meshtastic.db"
RECONNECT_DELAY = 5  # seconds before reconnect attempt

# Shared state
messages = []            # list of tuples: (timestamp, source, text)
msg_lock = threading.Lock()
scroll_pos = 0          # how many messages back from the bottom to start view
iface = None            # SerialInterface instance
db_conn = None          # sqlite3 connection


def setup_db():
    """Initialize SQLite database and return connection"""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    c.execute(
        '''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL,
            src TEXT,
            text TEXT
        )
        '''
    )
    conn.commit()
    return conn


def insert_message(conn, timestamp, src, text):
    """Insert a received message into the database"""
    try:
        c = conn.cursor()
        c.execute(
            'INSERT INTO messages (timestamp, src, text) VALUES (?, ?, ?)',
            (timestamp, src, text)
        )
        conn.commit()
    except Exception:
        # In production, log somewhere; here we just ignore db failures
        pass


def on_receive(packet: Packet = None, interface=None, **kwargs):
    """Meshtastic callback for incoming packets"""
    try:
        ts = time.time()
        # source user or node ID
        src = packet.uplink or packet.from_node or "unknown"
        # decoded text
        text = packet.decoded.text if packet.decoded and hasattr(packet.decoded, 'text') else ''
        with msg_lock:
            messages.append((ts, src, text))
        insert_message(db_conn, ts, src, text)
    except Exception:
        # keep UI alive on callback error
        traceback.print_exc()


def connect():
    """Attempt to connect to the Meshtastic node, retry on failure"""
    global iface
    while True:
        try:
            iface = SerialInterface(devPath=DEV_PATH)
            iface.onReceive += on_receive
            return
        except Exception as e:
            print(f"[!] Connection failed: {e}. Retrying in {RECONNECT_DELAY}s...")
            time.sleep(RECONNECT_DELAY)


def input_message(stdscr):
    """Prompt user for a message and send via Meshtastic"""
    global iface
    height, width = stdscr.getmaxyx()
    prompt = "Enter message: "
    curses.echo()
    curses.curs_set(1)
    stdscr.addstr(height - 1, 0, prompt)
    stdscr.clrtoeol()
    msg = stdscr.getstr(height - 1, len(prompt), width - len(prompt) - 1)
    curses.noecho()
    curses.curs_set(0)
    try:
        text = msg.decode('utf-8')
        iface.sendText(text)
    except Exception:
        pass


def draw_ui(stdscr):
    """Render messages list and UI controls"""
    global scroll_pos
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    display_height = height - 2
    with msg_lock:
        total = len(messages)
        start = max(0, total - display_height - scroll_pos)
        end = max(0, total - scroll_pos)
        view = messages[start:end]
    for idx, (ts, src, text) in enumerate(view[-display_height:]):
        timestamp = time.strftime('%H:%M:%S', time.localtime(ts))
        line = f"{timestamp} {src}: {text}"[:width - 1]
        stdscr.addstr(idx, 0, line)
    # draw separator and help
    stdscr.hline(display_height, 0, '-', width)
    help_text = "s: send | ↑/↓: scroll | Ctrl+C: exit"
    stdscr.addstr(display_height + 1, 0, help_text[:width - 1])
    stdscr.refresh()


def main(stdscr):
    """Main entry point for curses wrapper"""
    global db_conn, scroll_pos
    curses.curs_set(0)
    curses.use_default_colors()
    stdscr.nodelay(True)
    db_conn = setup_db()
    connect()
    # run UI loop
    while True:
        draw_ui(stdscr)
        try:
            key = stdscr.getch()
            if key == ord('s'):
                input_message(stdscr)
            elif key == curses.KEY_UP:
                scroll_pos = min(scroll_pos + 1, max(0, len(messages) - 1))
            elif key == curses.KEY_DOWN:
                scroll_pos = max(scroll_pos - 1, 0)
            time.sleep(0.1)
        except KeyboardInterrupt:
            break
        except Exception:
            # swallow unexpected UI errors
            traceback.print_exc()


if __name__ == '__main__':
    curses.wrapper(main)
