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
import curses.textpad
from pathlib import Path
from datetime import datetime
from meshtastic.ble_interface import BLEInterface
from pubsub import pub
import asyncio
from bleak import BleakScanner

# ── CONFIG ───────────────────────────────────────────────────────────────────
DATA_DIR = Path.home() / ".retrobadge"; DATA_DIR.mkdir(exist_ok=True)
DB_FILE  = DATA_DIR / "meshtastic.db"
LOG_FILE = DATA_DIR / "meshtastic.log"
NODE_ADDR = os.getenv("MESHTASTIC_BLE_ADDR", "NOT_CONFIGURED")  # Will be set by run_badge.sh
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
last_activity_time = 0  # Track last successful activity
discovered_devices = {}  # Cache discovered devices

db_q = queue.Queue()
def db_writer():
    while True:
        ts, src, txt = db_q.get()
        with db:
            db.execute(
                "INSERT INTO messages (ts, src, txt) VALUES (?,?,?)",
                (ts, src, txt),
            )
        db_q.task_done()

threading.Thread(target=db_writer, daemon=True).start()


# ── SIMPLE MESSAGE HANDLER ──────────────────────────────────────────────────
def simple_message_handler(packet, interface=None, topic=pub.AUTO_TOPIC):
    global last_activity_time
    # writeflush helper
    def log(line):
        json_fh.write(line + "\n")
        json_fh.flush()

    log(f"# PACKET: topic={topic} packet={packet}")
    # Update activity time when we receive messages
    last_activity_time = time.time()
    try:
        # --- extract text, src, ts exactly as before ---
        txt_field = None
        if isinstance(packet, dict):
            dec = packet.get("decoded", {})
            txt_field = dec.get("text") or dec.get("data", {}).get("text")
            if txt_field is None and dec.get("data", {}).get("payload"):
                try:
                    pl = dec["data"]["payload"]
                    txt_field = (bytes(pl) if isinstance(pl, list) else pl).decode("utf-8", "ignore")
                except Exception:
                    pass
            src = packet.get("fromId", "unknown")
            ts  = packet.get("rxTime", time.time())
        else:
            dec = getattr(packet, "decoded", None)
            if dec:
                txt_field = getattr(dec, "text", None)
                data = getattr(dec, "data", None)
                if txt_field is None and data:
                    txt_field = getattr(data, "text", None)
                    if txt_field is None and hasattr(data, "payload"):
                        try:
                            txt_field = bytes(data.payload).decode("utf-8", "ignore")
                        except Exception:
                            pass
            src = getattr(packet, "fromId", "unknown")
            ts  = getattr(packet, "rxTime", time.time())

        # --- bail if no text ---
        if not txt_field:
            return

        # ms→s sanity
        if ts > 1e12:
            ts /= 1000
        text = txt_field[:MAX_LEN]

        log(f"# Received: {src}: {text}")

        # 1) enqueue to UI (non-blocking)
        try:
            incoming_q.put_nowait((ts, src, text))
        except queue.Full:
            # UI is backed up, drop
            pass

        # 2) enqueue to DB writer (blocking if it ever needs to)
        db_q.put((ts, src, text))

    except Exception as e:
        log(f"# Message handler error: {e}")


def on_conn_established(interface=None, topic=pub.AUTO_TOPIC, **kwargs):
    link_up_evt.set()
    json_fh.write("# CONNECTION ESTABLISHED\n")

def on_conn_lost(interface=None, topic=pub.AUTO_TOPIC, **kwargs):
    link_up_evt.clear()
    json_fh.write("# CONNECTION LOST\n")

# ── PUBSUB SUBSCRIPTIONS ─────────────────────────────────────────────────────
pub.subscribe(simple_message_handler,        "meshtastic.receive")       
pub.subscribe(on_conn_established,           "meshtastic.connection.established")
pub.subscribe(on_conn_lost,                  "meshtastic.connection.lost")

# ── RADIO THREAD ──────────────────────────────────────────────────────────────

def _radio_worker():
    global _iface, connection_status, last_connection_attempt, last_activity_time, discovered_devices
    
    # Use the explicit address provided or discover
    configured_addr = NODE_ADDR.strip() if NODE_ADDR and NODE_ADDR != "NOT_CONFIGURED" else None
    json_fh.write(f"# Configured address: '{configured_addr}'\n")
    
    retry_count = 0
    max_retries = 10
    
    while not stop_evt.is_set() and retry_count < max_retries:
        try:
            device_addr = None
            
            # If we have a configured address, try it first
            if configured_addr:
                device_addr = configured_addr
                json_fh.write(f"# Using configured address: {device_addr}\n")
            else:
                # Auto-discover like the old working code
                json_fh.write("# No configured address, scanning for Meshtastic devices...\n")
                connection_status = "Scanning for Meshtastic devices..."
                
                try:
                    devices = asyncio.run(BleakScanner.discover(timeout=10.0))
                    for d in devices:
                        if d.name and "meshtastic" in d.name.lower():
                            device_addr = d.address
                            discovered_devices[d.address] = d.name
                            json_fh.write(f"# Found Meshtastic device: {d.name} at {device_addr}\n")
                            break
                except Exception as e:
                    json_fh.write(f"# BLE scan error: {e}\n")
                
                if not device_addr:
                    json_fh.write("# No Meshtastic devices found in scan\n")
                    connection_status = "No Meshtastic devices found"
                    retry_count += 1
                    time.sleep(10)
                    continue
            
            last_connection_attempt = time.time()
            connection_status = f"Connecting to {device_addr}..."
            json_fh.write(f"# Connection attempt {retry_count + 1} to {device_addr}\n")
            
            # Clear previous state
            link_up_evt.clear()
            
            # Create interface - simplified like the old code
            json_fh.write(f"# Creating BLE interface for {device_addr}...\n")
            with _iface_lock:
                _iface = BLEInterface(address=device_addr, debugOut=json_fh)
            
            json_fh.write("# Interface created successfully\n")
            connection_status = "Interface created, waiting for connection..."
            
            # Wait a moment for connection to establish
            time.sleep(3)
            
            # Simple connection verification - just try to get basic info once
            try:
                with _iface_lock:
                    if _iface:
                        my_info = _iface.getMyNodeInfo()
                        if my_info:
                            node_name = my_info.get('user', {}).get('longName', 'Unknown')
                            json_fh.write(f"# Connected to: {node_name}\n")
                            connection_status = f"Connected to {node_name}!"
                            link_up_evt.set()
                            last_activity_time = time.time()
                        else:
                            json_fh.write("# getMyNodeInfo returned None\n")
            except Exception as e:
                json_fh.write(f"# Initial connection test failed: {e}\n")
                # Don't fail immediately - the interface might still work for messages
                link_up_evt.set()  # Give it a chance
                last_activity_time = time.time()
                connection_status = "Connected (verification pending)"
            
            if link_up_evt.is_set():
                json_fh.write("# Entering message loop\n")
                retry_count = 0  # Reset on success
                
                # Simplified message loop - more like the old code
                while not stop_evt.is_set():
                    connection_healthy = True
                    
                    # Much less aggressive health checking - only if no activity for 60+ seconds
                    current_time = time.time()
                    if current_time - last_activity_time > 60:
                        json_fh.write("# Checking connection health (60s+ no activity)\n")
                        try:
                            with _iface_lock:
                                if _iface:
                                    # Just ping - don't overdo it
                                    _iface.getMyNodeInfo()
                                    last_activity_time = current_time
                                    json_fh.write("# Health check passed\n")
                        except Exception as e:
                            json_fh.write(f"# Health check failed: {e}\n")
                            connection_healthy = False
                    
                    # Handle outgoing messages
                    try:
                        msg = outgoing_q.get(timeout=1.0)
                        json_fh.write(f"# Sending: {msg}\n")
                        connection_status = f"Sending: {msg[:20]}..."
                        
                        with _iface_lock:
                            if _iface:
                                try:
                                    # Simple send - let the interface handle the details
                                    _iface.sendText(msg)
                                    json_fh.write(f"# Message sent\n")
                                    connection_status = "Message sent!"
                                    last_activity_time = time.time()
                                except Exception as send_err:
                                    json_fh.write(f"# Send error: {send_err}\n")
                                    connection_status = f"Send error: {send_err}"
                                    connection_healthy = False
                                
                    except queue.Empty:
                        # No outgoing messages
                        if link_up_evt.is_set():
                            device_name = discovered_devices.get(device_addr, device_addr)
                            connection_status = f"Connected to {device_name}"
                        continue
                    except Exception as e:
                        json_fh.write(f"# Message loop error: {e}\n")
                        connection_healthy = False
                    
                    if not connection_healthy:
                        json_fh.write("# Connection unhealthy, will reconnect\n")
                        break
                        
                    # Brief pause to prevent busy loop
                    time.sleep(0.1)
                
                # Connection lost
                link_up_evt.clear()
                connection_status = "Connection lost"
                retry_count += 1
                
            else:
                json_fh.write("# Connection failed\n")
                connection_status = "Connection failed"
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
                # Shorter waits for faster retry
                wait_time = 5 + min(retry_count * 2, 15)
                json_fh.write(f"# Retrying in {wait_time}s...\n")
                connection_status = f"Retrying in {wait_time}s..."
                stop_evt.wait(wait_time)
    
    if retry_count >= max_retries:
        json_fh.write(f"# Max retries exceeded\n")
        connection_status = f"Max retries exceeded"

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
    # Core curses setup
    curses.curs_set(0)
    curses.noecho()
    curses.cbreak()
    stdscr.keypad(True)
    stdscr.nodelay(True)
    
    # Colors
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_RED, curses.COLOR_BLACK)
    curses.init_pair(3, curses.COLOR_BLUE, curses.COLOR_BLACK)
    curses.init_pair(4, curses.COLOR_YELLOW, curses.COLOR_BLACK)
    text_col = curses.color_pair(1)
    no_link  = curses.color_pair(2)
    yes_link = curses.color_pair(3)
    warn_col = curses.color_pair(4)

    # Initial history and scroll position
    msgs = _history()
    h, w = stdscr.getmaxyx()
    pane_h = h - PAD_V*2 - 2
    viewofs = max(0, len(msgs) - pane_h)
    send_mode = False
    inp = ""
    TITLE = " Retro-Meshtastic Badge — Touch or ↑/↓ to scroll "

    while not stop_evt.is_set():
        # 1) Auto-scroll logic
        h, w = stdscr.getmaxyx()
        pane_h = h - PAD_V*2 - 2
        was_bottom = (viewofs >= len(msgs) - pane_h)
        new_msgs = False
        try:
            while True:
                msgs.append(incoming_q.get_nowait())
                new_msgs = True
        except queue.Empty:
            pass
        if was_bottom and new_msgs:
            viewofs = max(0, len(msgs) - pane_h)

        # 2) Draw frame
        stdscr.erase()
        stdscr.addstr(0, 0, "╔" + TITLE.center(w-2, "═")[:w-2] + "╗", text_col)

        # Connection status bar
        if link_up_evt.is_set():
            safe_footer(stdscr, 1, f"[● LINKED] Connected to {NODE_ADDR}", yes_link)
            row_start = PAD_V + 2
        else:
            safe_footer(stdscr, 1, f"[○ NO LINK] {connection_status[:w-5]}", no_link)
            if h > 10:
                debug = f"Last attempt: {int(time.time() - last_connection_attempt)}s ago"
                safe_footer(stdscr, 2, debug[:w-1], warn_col)
                row_start = PAD_V + 3
            else:
                row_start = PAD_V + 2

        # 3) Render message history
        row, used, idx = row_start, 0, viewofs
        while used < pane_h and idx < len(msgs):
            ts, src, txt = msgs[idx]
            safe_src = (src or "")[:10]
            prefix = f"{_fmt(ts)} {safe_src:>10} │ "
            avail = w - len(prefix)
            for j, line in enumerate(textwrap.wrap(txt, width=avail) or [""]):
                if used >= pane_h:
                    break
                line_out = (prefix + line if j == 0 else ' ' * len(prefix) + line).ljust(w)[:w]
                stdscr.addstr(row + used, 0, line_out, text_col)
                used += 1
            idx += 1

        stdscr.addstr(h-2, 0, "╚" + "═"*(w-2) + "╝", text_col)

        # 4) Footer / send prompt
        if send_mode:
            prompt = f"Send> {inp}"
            safe_footer(stdscr, h-1, prompt, text_col)
            stdscr.move(h-1, min(len(prompt), w-2))
        else:
            footer = "[S]end  [Ctrl-C/Q] quit  ↑/↓ PgUp/PgDn  Touch scroll"
            safe_footer(stdscr, h-1, footer, text_col)

        stdscr.refresh()
        curses.napms(100)

        # 5) Handle input
        try:
            c = stdscr.getch()
        except curses.error:
            c = -1

        # Quit
        if c in (3, ord('q'), ord('Q')):
            stop_evt.set()
            return

        # Enter send mode
        if c in (ord('s'), ord('S')) and not send_mode:
            send_mode, inp = True, ""
            continue

        # Send-mode textbox
        if send_mode:
            curses.curs_set(1)
            h, w = stdscr.getmaxyx()
            prompt = "Send> "
            stdscr.addstr(h-1, 0, prompt, text_col); stdscr.clrtoeol(); stdscr.refresh()
            win = curses.newwin(1, w - len(prompt) - 1, h-1, len(prompt))
            win.keypad(True)
            tb = curses.textpad.Textbox(win, insert_mode=True)
            def validator(ch): return 7 if ch in (10, 13) else ch
            try:
                s = tb.edit(validator).strip()
            finally:
                curses.curs_set(0)
            send_mode = False
            if s:
                ts = time.time()
                db_q.put((ts, "You", s))           # hand off to the writer thread
                msgs.append((ts, "You", s))
                outgoing_q.put(s)
            continue

        # Navigation keys
        if c == curses.KEY_UP:
            viewofs = max(0, viewofs-1)
        elif c == curses.KEY_DOWN:
            viewofs = min(len(msgs)-pane_h, viewofs+1)
        elif c == curses.KEY_PPAGE:
            viewofs = max(0, viewofs-pane_h)
        elif c == curses.KEY_NPAGE:
            viewofs = min(len(msgs)-pane_h, viewofs+pane_h)


        # Clamp scroll range
        viewofs = max(0, min(viewofs, max(0, len(msgs) - pane_h)))
# ── Entrypoint ───────────────────────────────────────────────────────────────
def _sig(*_):
    stop_evt.set()

def main():
    signal.signal(signal.SIGINT,  _sig)
    signal.signal(signal.SIGTERM, _sig)
    threading.Thread(target=_radio_worker, daemon=True).start()
    try:
        curses.wrapper(_ui)
    except KeyboardInterrupt:
        pass
    except Exception:
        # swallow other UI errors so your BLE thread can keep running
        pass
    stop_evt.set()
    json_fh.close()
    db.close()


if __name__ == "__main__":
    main()
