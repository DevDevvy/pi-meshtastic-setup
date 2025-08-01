#!/usr/bin/env python3
"""
meshtastic-retro-ui.py

Retro Meshtastic Badge – 3.5” Touch, Headless Edition
• Full-width title bar with link status in color
• Scrollable, wrapped message list
• Send mode (S → type → Enter)
• Quit with Ctrl-C or Q
• Persists to ~/.retrobadge/{meshtastic.db,meshtastic.log}
"""
import os, json, sqlite3, signal, queue, threading, time, curses, textwrap
from pathlib import Path
from datetime import datetime
from meshtastic.ble_interface import BLEInterface

# ── CONFIG ───────────────────────────────────────────────────────────────────
DATA_DIR = Path.home() / ".retrobadge"; DATA_DIR.mkdir(exist_ok=True)
DB_FILE  = DATA_DIR / "meshtastic.db"
LOG_FILE = DATA_DIR / "meshtastic.log"
NODE_ADDR = os.getenv("MESHTASTIC_BLE_ADDR", "48:CA:43:3C:51:FD")  # change to your node's BLE MAC
MAX_LEN, PAD_V = 240, 2  # truncate length, vertical padding

# ── PERSISTENCE ─────────────────────────────────────────────────────────────
json_fh = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
db      = sqlite3.connect(DB_FILE, check_same_thread=False)
with db:
    db.execute("""
      CREATE TABLE IF NOT EXISTS messages (
        ts   REAL,
        src  TEXT,
        txt  TEXT
      )""")

# ── SHARED STATE ─────────────────────────────────────────────────────────────
incoming_q  = queue.Queue(1024)
outgoing_q  = queue.Queue(256)
link_up_evt = threading.Event()
stop_evt    = threading.Event()
_iface_lock = threading.Lock()
_iface      = None
connection_status = "Initializing..."  # For UI display
last_connection_attempt = 0

# ── SIMPLE MESSAGE HANDLER ──────────────────────────────────────────────────
def simple_message_handler(packet, interface):
    """Simple message handler without pubsub"""
    try:
        # Check if this is a text message
        if hasattr(packet, 'decoded') and hasattr(packet.decoded, 'text'):
            text = packet.decoded.text[:MAX_LEN]
            from_id = getattr(packet, 'fromId', 'unknown')
            ts = getattr(packet, 'rxTime', time.time())
            if ts > 1e12:
                ts /= 1000
            
            json_fh.write(f"# Received: {from_id}: {text}\n")
            with db:
                db.execute("INSERT INTO messages VALUES (?,?,?)", (ts, from_id, text))
            incoming_q.put((ts, from_id, text))
    except Exception as e:
        json_fh.write(f"# Message handler error: {e}\n")

# ── RADIO THREAD ──────────────────────────────────────────────────────────────
def _find_ble_node():
    json_fh.write("# Scanning for BLE devices…\n")
    try:
        devices = BLEInterface.scan()
        json_fh.write(f"# Found {len(devices)} BLE devices.\n")
        for d in devices:
            json_fh.write(f"#   • {d.name} @ {d.address}\n")
        return devices[0].address if devices else None
    except Exception as e:
        json_fh.write(f"# BLE scan error: {e}\n")
        return None

def _radio_worker():
    global _iface, connection_status, last_connection_attempt
    
    # Try to find the device first
    addr = NODE_ADDR
    if not addr or addr == "48:CA:43:3C:51:FD":  # Default placeholder
        json_fh.write("# No specific address set, scanning for devices...\n")
        connection_status = "Scanning for devices..."
        addr = _find_ble_node()
        if not addr:
            json_fh.write("# No BLE devices found\n")
            connection_status = "No BLE devices found"
            return
    
    json_fh.write(f"# Using BLE address: {addr}\n")
    connection_status = f"Found device: {addr}"
    
    retry_count = 0
    max_retries = 5
    
    while not stop_evt.is_set() and retry_count < max_retries:
        try:
            last_connection_attempt = time.time()
            connection_status = f"Connecting to {addr}... ({retry_count + 1}/{max_retries})"
            json_fh.write(f"# Connection attempt {retry_count + 1}: {addr}\n")
            
            # Clear previous state
            link_up_evt.clear()
            
            # Create interface simply
            json_fh.write("# Creating BLE interface...\n")
            with _iface_lock:
                _iface = BLEInterface(address=addr, debugOut=json_fh)
                # Set up simple message handler
                _iface.onReceive = simple_message_handler
            
            json_fh.write("# Interface created, testing connection...\n")
            connection_status = "Testing connection..."
            
            # Test connection by getting node info
            connection_test_count = 0
            max_test_attempts = 10
            
            for i in range(max_test_attempts):
                try:
                    with _iface_lock:
                        if _iface:
                            my_info = _iface.getMyNodeInfo()
                            if my_info:
                                node_name = my_info.get('user', {}).get('longName', 'Unknown')
                                json_fh.write(f"# Connected to: {node_name}\n")
                                connection_status = f"Connected to {node_name}!"
                                link_up_evt.set()
                                break
                except Exception as e:
                    json_fh.write(f"# Connection test {i+1} failed: {e}\n")
                
                connection_status = f"Testing connection... ({i+1}/{max_test_attempts})"
                time.sleep(2)
                
                if stop_evt.is_set():
                    break
            
            if link_up_evt.is_set():
                json_fh.write("# Connection established, entering message loop\n")
                retry_count = 0  # Reset on success
                
                # Main message loop
                while not stop_evt.is_set() and link_up_evt.is_set():
                    try:
                        # Handle outgoing messages
                        msg = outgoing_q.get(timeout=2.0)
                        json_fh.write(f"# Sending: {msg}\n")
                        connection_status = f"Sending: {msg[:20]}..."
                        
                        with _iface_lock:
                            if _iface:
                                _iface.sendText(msg)
                                connection_status = "Message sent!"
                                
                    except queue.Empty:
                        # No outgoing messages, just keep connection alive
                        connection_status = "Connected (idle)"
                        continue
                    except Exception as e:
                        json_fh.write(f"# Send error: {e}\n")
                        connection_status = f"Send error: {e}"
                        break
            else:
                json_fh.write("# Connection test failed\n")
                connection_status = "Connection test failed"
                retry_count += 1
                
        except Exception as e:
            json_fh.write(f"# Radio worker error: {e}\n")
            import traceback
            json_fh.write(f"# Traceback: {traceback.format_exc()}\n")
            retry_count += 1
            connection_status = f"Error: {str(e)[:50]}..."
            
        finally:
            # Cleanup
            link_up_evt.clear()
            with _iface_lock:
                if _iface:
                    try:
                        _iface.close()
                        json_fh.write("# Interface closed\n")
                    except Exception as e:
                        json_fh.write(f"# Close error: {e}\n")
                    _iface = None
            
            if not stop_evt.is_set() and retry_count < max_retries:
                wait_time = 10 + retry_count * 5
                json_fh.write(f"# Retrying in {wait_time}s...\n")
                connection_status = f"Retrying in {wait_time}s..."
                stop_evt.wait(wait_time)
    
    if retry_count >= max_retries:
        json_fh.write(f"# Max retries exceeded\n")
        connection_status = "Max retries exceeded"

# ── HELPERS ──────────────────────────────────────────────────────────────────
def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _history(limit=2000):
    cur = db.cursor()
    cur.execute("SELECT ts, src, txt FROM messages ORDER BY ts DESC LIMIT ?", (limit,))
    return list(reversed(cur.fetchall()))


def safe_footer(win, row: int, text: str, attr=0):
    h, w = win.getmaxyx()
    safe = text.ljust(w-1)[:w-1]
    try:
        win.addstr(row, 0, safe, attr)
    except curses.error:
        pass

# ── CURSES UI ─────────────────────────────────────────────────────────────────
def _ui(stdscr):
    curses.curs_set(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_RED,   curses.COLOR_BLACK)
    curses.init_pair(3, curses.COLOR_BLUE,  curses.COLOR_BLACK)
    curses.init_pair(4, curses.COLOR_YELLOW, curses.COLOR_BLACK)

    text_col = curses.color_pair(1)
    no_link  = curses.color_pair(2)
    yes_link = curses.color_pair(3)
    warn_col = curses.color_pair(4)

    msgs    = _history()
    viewofs = max(0, len(msgs) - (curses.LINES - PAD_V*2 - 2))
    send_mode = False
    inp = ""

    TITLE = " Retro-Meshtastic Badge — Touch or ↑/↓ to scroll "

    while not stop_evt.is_set():
        # drain incoming messages
        try:
            while True:
                msgs.append(incoming_q.get_nowait())
                # Auto-scroll to new messages
                h, w = stdscr.getmaxyx()
                pane_h = h - PAD_V*2 - 2
                viewofs = max(0, len(msgs) - pane_h)
        except queue.Empty:
            pass

        h, w = stdscr.getmaxyx()
        pane_h = h - PAD_V*2 - 2
        if viewofs >= max(0, len(msgs) - pane_h):
            viewofs = max(0, len(msgs) - pane_h)

        stdscr.erase()
        stdscr.addstr(0, 0, "╔" + TITLE.center(w-2, "═")[:w-2] + "╗", text_col)
        
        # Show detailed connection status
        if link_up_evt.is_set():
            status = "[● LINKED]"
            status_attr = yes_link
        else:
            status = "[○ NO LINK]"
            status_attr = no_link
        
        # Add detailed status on second line
        detailed_status = f"Status: {connection_status}"[:w-1]
        safe_footer(stdscr, 1, status.center(w-1), status_attr)
        
        # Add a third line for detailed connection info
        if h > 10:  # Only if we have enough screen space
            time_since_attempt = time.time() - last_connection_attempt if last_connection_attempt > 0 else 0
            debug_info = f"Last attempt: {time_since_attempt:.0f}s ago | {detailed_status}"[:w-1]
            safe_footer(stdscr, 2, debug_info, warn_col)
            # Adjust message area to account for extra status line
            pane_h -= 1
            row_start = PAD_V + 3
        else:
            row_start = PAD_V + 2

        # render wrapped history
        row, used, idx = row_start, 0, viewofs
        while used < pane_h and idx < len(msgs):
            ts, src, txt = msgs[idx]
            prefix = f"{_fmt(ts)} {src[:10]:>10} │ "
            avail = w - len(prefix)
            for j, line in enumerate(textwrap.wrap(txt, width=avail) or [""]):
                if used >= pane_h: break
                if j == 0:
                    line_out = (prefix + line).ljust(w)[:w]
                else:
                    line_out = (" " * len(prefix) + line).ljust(w)[:w]
                stdscr.addstr(row + used, 0, line_out, text_col)
                used += 1
            idx += 1

        stdscr.addstr(h-2, 0, "╚" + "═"*(w-2) + "╝", text_col)
        if send_mode:
            prompt = f"Send> {inp}"
            safe_footer(stdscr, h-1, prompt, text_col)
            stdscr.move(h-1, min(len(prompt), w-2))
        else:
            footer = "[S]end  [Ctrl-C/Q] quit  ↑/↓ PgUp/PgDn  Touch scroll"
            safe_footer(stdscr, h-1, footer, text_col)

        stdscr.refresh()
        curses.napms(100)

        try:
            c = stdscr.getch()
        except curses.error:
            continue

        if send_mode:
            if c in (10, 13):  # Enter
                msg = inp.strip()
                send_mode, inp = False, ""
                if msg:
                    ts = time.time()
                    with db:
                        db.execute("INSERT INTO messages VALUES (?,?,?)", (ts, "You", msg))
                    msgs.append((ts, "You", msg))
                    outgoing_q.put(msg)
            elif c in (27,):    # Esc
                send_mode = False
            elif c in (127, 8): # Backspace
                inp = inp[:-1]
            elif 32 <= c <= 126:
                inp += chr(c)
            continue

        # navigation keys
        if c == curses.KEY_UP:
            viewofs = max(0, viewofs - 1)
        elif c == curses.KEY_DOWN:
            viewofs = min(len(msgs)-pane_h, viewofs + 1)
        elif c == curses.KEY_PPAGE:
            viewofs = max(0, viewofs - pane_h)
        elif c == curses.KEY_NPAGE:
            viewofs = min(len(msgs)-pane_h, viewofs + pane_h)
        elif c == curses.KEY_MOUSE:
            _, mx, my, _, b = curses.getmouse()
            if b & curses.BUTTON4_PRESSED:
                viewofs = max(0, viewofs - 3)
            if b & curses.BUTTON5_PRESSED:
                viewofs = min(len(msgs)-pane_h, viewofs + 3)
        elif c in (ord('s'), ord('S')):
            send_mode, inp = True, ""
        elif c in (ord('q'), ord('Q')):
            stop_evt.set()

# ── Entrypoint ───────────────────────────────────────────────────────────────
def _sig(*_):
    stop_evt.set()


def main():
    signal.signal(signal.SIGINT,  _sig)
    signal.signal(signal.SIGTERM, _sig)
    threading.Thread(target=_radio_worker, daemon=True).start()
    curses.wrapper(_ui)
    stop_evt.set()
    json_fh.close()
    db.close()


if __name__ == "__main__":
    main()
