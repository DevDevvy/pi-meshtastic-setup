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
connection_status = "Initializing..."  # For UI display
last_connection_attempt = 0

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
    global _iface, connection_status, last_connection_attempt
    addr = NODE_ADDR or _find_ble_node()
    if not addr:
        json_fh.write("# No BLE address available\n")
        connection_status = "No BLE address"
        return
    
    retry_count = 0
    max_retries = 10
    
    while not stop_evt.is_set() and retry_count < max_retries:
        try:
            last_connection_attempt = time.time()
            connection_status = f"Connecting... (attempt {retry_count + 1}/{max_retries})"
            json_fh.write(f"# Attempt {retry_count + 1}/{max_retries}: Connecting to {addr}\n")
            
            # Clear any previous connection state
            link_up_evt.clear()
            
            # Create interface with timeout handling
            json_fh.write("# Creating BLE interface...\n")
            connection_status = "Creating BLE interface..."
            
            # Use a thread to create the interface with a timeout
            interface_created = threading.Event()
            interface_error = [None]  # List to store error from thread
            
            def create_interface():
                try:
                    json_fh.write(f"# Attempting BLEInterface(address={addr})\n")
                    new_iface = BLEInterface(address=addr, debugOut=json_fh, noProto=False, noNodes=False)
                    with _iface_lock:
                        global _iface
                        _iface = new_iface
                    json_fh.write("# BLEInterface created successfully\n")
                    interface_created.set()
                except Exception as e:
                    json_fh.write(f"# BLEInterface creation failed: {e}\n")
                    import traceback
                    json_fh.write(f"# Creation traceback: {traceback.format_exc()}\n")
                    interface_error[0] = e
                    interface_created.set()
            
            # Start the interface creation in a separate thread
            create_thread = threading.Thread(target=create_interface, daemon=True)
            create_thread.start()
            
            # Wait for interface creation with timeout
            interface_timeout = 30
            json_fh.write(f"# Waiting up to {interface_timeout}s for interface creation...\n")
            
            for i in range(interface_timeout):
                if interface_created.wait(timeout=1):
                    break
                if stop_evt.is_set():
                    json_fh.write("# Stop event set during interface creation\n")
                    break
                connection_status = f"Creating interface... ({i+1}/{interface_timeout}s)"
                json_fh.write(f"# Interface creation wait: {i+1}/{interface_timeout}s\n")
            
            # Check if interface creation failed
            if interface_error[0]:
                raise interface_error[0]
            
            if not interface_created.is_set():
                raise TimeoutError(f"BLE interface creation timed out after {interface_timeout}s")
            
            with _iface_lock:
                if not _iface:
                    raise RuntimeError("BLE interface was not created properly")
            
            json_fh.write("# BLE interface created successfully\n")
            connection_status = "Interface created, handshaking..."
            
            # Give time for the initial connection handshake
            connection_wait_time = 15
            json_fh.write(f"# Waiting {connection_wait_time}s for connection handshake...\n")
            
            # Check for connection in smaller intervals
            for i in range(connection_wait_time):
                if stop_evt.is_set():
                    break
                    
                connection_status = f"Handshake... ({i+1}/{connection_wait_time}s)"
                
                # Check if pubsub events have set the link up
                if link_up_evt.is_set():
                    json_fh.write(f"# Connection established via pubsub after {i+1}s\n")
                    connection_status = "Connected via pubsub!"
                    break
                    
                # Also try to verify by attempting to get basic info
                try:
                    with _iface_lock:
                        if _iface:
                            json_fh.write(f"# Attempting getMyNodeInfo() at {i+1}s...\n")
                            # Try to get my node info as a connection test
                            my_info = _iface.getMyNodeInfo()
                            if my_info and 'user' in my_info:
                                json_fh.write(f"# Connection verified via getMyNodeInfo after {i+1}s\n")
                                json_fh.write(f"# Node info: {my_info}\n")
                                link_up_evt.set()
                                connection_status = "Connected via node info!"
                                break
                            else:
                                json_fh.write(f"# getMyNodeInfo returned: {my_info}\n")
                except Exception as e:
                    json_fh.write(f"# getMyNodeInfo attempt {i+1} failed: {e}\n")
                    
                time.sleep(1)
            
            # Final connection verification
            connection_verified = False
            if link_up_evt.is_set():
                connection_verified = True
                connection_status = "Connection verified!"
            else:
                # Try one more time to verify connection
                connection_status = "Final verification..."
                try:
                    with _iface_lock:
                        if _iface:
                            json_fh.write("# Final connection verification attempt...\n")
                            my_info = _iface.getMyNodeInfo()
                            json_fh.write(f"# Final getMyNodeInfo result: {my_info}\n")
                            if my_info:
                                user_name = my_info.get('user', {}).get('longName', 'Unknown')
                                node_id = my_info.get('user', {}).get('id', 'Unknown')
                                json_fh.write(f"# Connection verified! Node: {user_name} ({node_id})\n")
                                link_up_evt.set()
                                connection_verified = True
                                connection_status = f"Connected to {user_name}!"
                            else:
                                json_fh.write("# getMyNodeInfo returned None\n")
                                connection_status = "Node info failed"
                except Exception as e:
                    json_fh.write(f"# Final verification failed: {e}\n")
                    connection_status = f"Verification failed: {e}"
            
            if connection_verified:
                json_fh.write("# Entering main communication loop\n")
                retry_count = 0  # Reset retry count on successful connection
                
                # Send a test message to verify sending works
                try:
                    with _iface_lock:
                        if _iface:
                            # Get some basic info to show we're really connected
                            nodes = _iface.nodes
                            json_fh.write(f"# Network has {len(nodes)} known nodes\n")
                            connection_status = f"Connected! {len(nodes)} nodes"
                except Exception as e:
                    json_fh.write(f"# Warning getting nodes: {e}\n")
                    connection_status = "Connected (node count failed)"
                
                # Main message loop
                last_activity = time.time()
                heartbeat_interval = 30  # Send a heartbeat every 30 seconds
                consecutive_errors = 0
                
                while not stop_evt.is_set() and consecutive_errors < 3:
                    try:
                        # Try to get a message with a timeout
                        msg = outgoing_q.get(timeout=5.0)
                        json_fh.write(f"# Sending message: '{msg}'\n")
                        connection_status = f"Sending: {msg[:20]}..."
                        
                        with _iface_lock:
                            if _iface:
                                # Send the message
                                _iface.sendText(msg)
                                json_fh.write("# Message sent successfully\n")
                                last_activity = time.time()
                                consecutive_errors = 0
                                connection_status = "Message sent!"
                                
                    except queue.Empty:
                        # No message to send - do periodic connection check
                        current_time = time.time()
                        
                        # Update status to show we're idle but connected
                        if link_up_evt.is_set():
                            connection_status = "Connected (idle)"
                        
                        # Send periodic heartbeat/connection check
                        if current_time - last_activity > heartbeat_interval:
                            try:
                                with _iface_lock:
                                    if _iface:
                                        # Just try to access a simple property to verify connection
                                        _ = _iface.nodes
                                        last_activity = current_time
                                        json_fh.write("# Connection heartbeat OK\n")
                                        connection_status = "Connected (heartbeat OK)"
                            except Exception as e:
                                json_fh.write(f"# Heartbeat failed: {e}\n")
                                consecutive_errors += 1
                                connection_status = f"Heartbeat failed: {e}"
                                
                    except Exception as e:
                        json_fh.write(f"# Send error: {e}\n")
                        consecutive_errors += 1
                        connection_status = f"Send error: {e}"
                        
                        # If we get repeated send errors, the connection is probably dead
                        if consecutive_errors >= 3:
                            json_fh.write("# Too many consecutive send errors, connection likely dead\n")
                            connection_status = "Connection dead (send errors)"
                            break
                            
                json_fh.write("# Exiting main communication loop\n")
                
            else:
                json_fh.write("# Connection verification failed\n")
                retry_count += 1
                connection_status = f"Verification failed (retry {retry_count})"
                
        except Exception as e:
            json_fh.write(f"# Radio worker error: {e}\n")
            import traceback
            json_fh.write(f"# Traceback: {traceback.format_exc()}\n")
            retry_count += 1
            connection_status = f"Error: {str(e)[:50]}..."
            
        finally:
            # Always clean up the interface
            json_fh.write("# Cleaning up interface\n")
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
                wait_time = min(10 + retry_count * 5, 60)  # Longer backoff
                json_fh.write(f"# Retrying in {wait_time}s... (attempt {retry_count}/{max_retries})\n")
                connection_status = f"Retrying in {wait_time}s..."
                stop_evt.wait(wait_time)
    
    if retry_count >= max_retries:
        json_fh.write(f"# Max retries ({max_retries}) exceeded, radio worker exiting\n")
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
