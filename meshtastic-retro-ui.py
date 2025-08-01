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

try:
    from pubsub import pub
    PUBSUB_AVAILABLE = True
except ImportError:
    PUBSUB_AVAILABLE = False

# ── CONFIG ───────────────────────────────────────────────────────────────────
DATA_DIR = Path.home() / ".retrobadge"; DATA_DIR.mkdir(exist_ok=True)
DB_FILE  = DATA_DIR / "meshtastic.db"
LOG_FILE = DATA_DIR / "meshtastic.log"
NODE_ADDR = os.getenv("MESHTASTIC_BLE_ADDR", "48:CA:43:3C:51:FD")  # change to your node's BLE MAC
MAX_LEN, PAD_V = 240, 2  # truncate length, vertical padding

# ── PERSISTENCE ─────────────────────────────────────────────────────────────
json_fh = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
if not PUBSUB_AVAILABLE:
    json_fh.write("# Warning: pubsub not available, message receiving may not work\n")
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

# ── BLE CALLBACKS ────────────────────────────────────────────────────────────
def on_receive_text(packet, interface=None):
    """Called when a text message is received via pubsub."""
    try:
        # Extract text from the packet
        if hasattr(packet, 'decoded') and hasattr(packet.decoded, 'text'):
            text = packet.decoded.text[:MAX_LEN]
        elif isinstance(packet, dict) and 'decoded' in packet and 'text' in packet['decoded']:
            text = packet['decoded']['text'][:MAX_LEN]
        else:
            return
        
        # Get sender info - try different ways to extract it
        from_id = 'unknown'
        if hasattr(packet, 'fromId'):
            from_id = packet.fromId
        elif isinstance(packet, dict) and 'fromId' in packet:
            from_id = packet['fromId']
        elif hasattr(packet, 'from'):
            from_id = f"!{packet['from']:08x}"
        elif isinstance(packet, dict) and 'from' in packet:
            from_id = f"!{packet['from']:08x}"
        
        # Get timestamp
        ts = time.time()
        if hasattr(packet, 'rxTime'):
            ts = packet.rxTime
        elif isinstance(packet, dict) and 'rxTime' in packet:
            ts = packet['rxTime']
        
        if ts > 1e12:
            ts /= 1000
        
        # Log and persist
        json_fh.write(f"# Received message from {from_id}: {text}\n")
        json_fh.write(json.dumps(packet, default=str) + "\n")
        
        with db:
            db.execute("INSERT INTO messages VALUES (?,?,?)", (ts, from_id, text))
        incoming_q.put((ts, from_id, text))
        
    except Exception as e:
        json_fh.write(f"# Error processing received packet: {e}\n")


def on_connection_established(interface=None, topic=None):
    """Called when BLE connection is established via pubsub."""
    link_up_evt.set()
    addr = getattr(interface, 'address', 'unknown') if interface else 'unknown'
    json_fh.write(f"# CONNECTION ESTABLISHED to {addr}\n")


def on_connection_lost(interface=None, topic=None):
    """Called when BLE connection is lost via pubsub."""
    link_up_evt.clear()
    addr = getattr(interface, 'address', 'unknown') if interface else 'unknown'
    json_fh.write(f"# CONNECTION LOST from {addr}\n")


# Set up pubsub subscriptions if available
if PUBSUB_AVAILABLE:
    pub.subscribe(on_receive_text, "meshtastic.receive.text")
    pub.subscribe(on_connection_established, "meshtastic.connection.established")
    pub.subscribe(on_connection_lost, "meshtastic.connection.lost")
    json_fh.write("# Pubsub callbacks registered\n")

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
    global _iface
    addr = NODE_ADDR or _find_ble_node()
    if not addr:
        json_fh.write("# No BLE address available\n")
        return
    
    retry_count = 0
    max_retries = 10
    
    while not stop_evt.is_set() and retry_count < max_retries:
        try:
            json_fh.write(f"# Attempt {retry_count + 1}/{max_retries}: Connecting to {addr}\n")
            
            # Create interface - this should trigger pubsub events
            with _iface_lock:
                _iface = BLEInterface(address=addr, debugOut=json_fh, noProto=False)
            
            json_fh.write("# BLE interface created, waiting for connection...\n")
            
            # Give time for initial connection and protocol handshake
            time.sleep(5)
            
            # Try to verify connection by getting node info
            connection_verified = False
            try:
                with _iface_lock:
                    if _iface:
                        json_fh.write("# Attempting to get node info...\n")
                        node_info = _iface.getMyNodeInfo()
                        if node_info:
                            user_name = node_info.get('user', {}).get('longName', 'Unknown')
                            json_fh.write(f"# Node verified: {user_name}\n")
                            link_up_evt.set()
                            connection_verified = True
                        else:
                            json_fh.write("# No node info received\n")
            except Exception as e:
                json_fh.write(f"# Error getting node info: {e}\n")
            
            if connection_verified or link_up_evt.is_set():
                json_fh.write("# Connection appears successful, entering message loop\n")
                retry_count = 0  # Reset retry count on successful connection
                
                # Main message loop - keep running while connected
                consecutive_errors = 0
                while not stop_evt.is_set() and consecutive_errors < 5:
                    try:
                        msg = outgoing_q.get(timeout=2.0)
                        json_fh.write(f"# Sending message: {msg}\n")
                        with _iface_lock:
                            if _iface:
                                _iface.sendText(msg)
                                json_fh.write("# Message sent successfully\n")
                                consecutive_errors = 0
                    except queue.Empty:
                        # No message to send, check if interface is still alive
                        try:
                            with _iface_lock:
                                if _iface and hasattr(_iface, 'client') and _iface.client:
                                    # Try a simple operation to test if connection is alive
                                    continue
                                else:
                                    json_fh.write("# Interface appears disconnected\n")
                                    break
                        except Exception as e:
                            json_fh.write(f"# Interface check error: {e}\n")
                            consecutive_errors += 1
                    except Exception as e:
                        json_fh.write(f"# Send error: {e}\n")
                        consecutive_errors += 1
                        if consecutive_errors >= 5:
                            json_fh.write("# Too many consecutive errors, reconnecting\n")
                            break
            else:
                json_fh.write("# Connection verification failed\n")
                retry_count += 1
                
        except Exception as e:
            json_fh.write(f"# Radio worker error: {e}\n")
            retry_count += 1
            
        finally:
            # Always clean up the interface
            link_up_evt.clear()
            with _iface_lock:
                if _iface:
                    try:
                        _iface.close()
                        json_fh.write("# Interface closed cleanly\n")
                    except Exception as e:
                        json_fh.write(f"# Error closing interface: {e}\n")
                    _iface = None
            
            # Wait before retry if not stopping
            if not stop_evt.is_set() and retry_count < max_retries:
                wait_time = min(5 + retry_count * 2, 30)  # Exponential backoff, max 30s
                json_fh.write(f"# Retrying in {wait_time}s...\n")
                stop_evt.wait(wait_time)
                
                # Try to find a new device if we don't have a fixed address
                if not NODE_ADDR:
                    new_addr = _find_ble_node()
                    if new_addr and new_addr != addr:
                        addr = new_addr
                        retry_count = 0  # Reset on new device
    
    if retry_count >= max_retries:
        json_fh.write(f"# Max retries ({max_retries}) exceeded, giving up\n")

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

    text_col = curses.color_pair(1)
    no_link  = curses.color_pair(2)
    yes_link = curses.color_pair(3)

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
        except queue.Empty:
            pass

        h, w = stdscr.getmaxyx()
        pane_h = h - PAD_V*2 - 2
        if viewofs >= max(0, len(msgs) - pane_h):
            viewofs = max(0, len(msgs) - pane_h)

        stdscr.erase()
        stdscr.addstr(0, 0, "╔" + TITLE.center(w-2, "═")[:w-2] + "╗", text_col)
        status = "[● LINKED]" if link_up_evt.is_set() else "[○ NO LINK]"
        safe_footer(stdscr, 1, status.center(w-1), yes_link if link_up_evt.is_set() else no_link)

        # render wrapped history
        row, used, idx = PAD_V + 2, 0, viewofs
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
        curses.napms(30)

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
