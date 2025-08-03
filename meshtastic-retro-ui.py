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
_iface      = None
connection_status = "Initializing..."  # For UI display
last_connection_attempt = 0

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
def direct_message_handler(packet):
    """Direct callback handler like the old working code"""
    json_fh.write(f"# DIRECT PACKET: {packet}\n")
    json_fh.flush()
    
    try:
        # Extract text from packet - simplified approach
        text = None
        if isinstance(packet, dict):
            decoded = packet.get('decoded', {})
            text = decoded.get('text')
            src = packet.get('fromId', 'unknown')
            ts = packet.get('rxTime', time.time())
        else:
            # Handle protobuf objects
            if hasattr(packet, 'decoded') and packet.decoded:
                text = getattr(packet.decoded, 'text', None)
            src = getattr(packet, 'fromId', 'unknown')
            ts = getattr(packet, 'rxTime', time.time())
        
        if not text:
            return
            
        # ms→s sanity
        if ts > 1e12:
            ts /= 1000
        text = text[:MAX_LEN]
        
        json_fh.write(f"# Direct received: {src}: {text}\n")
        json_fh.flush()
        
        # Enqueue to UI and DB
        try:
            incoming_q.put_nowait((ts, src, text))
        except queue.Full:
            pass
        db_q.put((ts, src, text))
        
    except Exception as e:
        json_fh.write(f"# Direct handler error: {e}\n")
        json_fh.flush()

def simple_message_handler(packet, interface=None, topic=pub.AUTO_TOPIC):
    # Keep this as backup, but the direct handler should handle most messages
    json_fh.write(f"# PUBSUB PACKET: topic={topic}\n")
    json_fh.flush()
    # Just call the direct handler
    direct_message_handler(packet)

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
    global _iface, connection_status, last_connection_attempt
    
    # Use the explicit address provided
    addr = NODE_ADDR
    json_fh.write(f"# NODE_ADDR from env: '{addr}'\n")
    json_fh.flush()
    
    # Check if we have a valid address
    if not addr or addr == "NOT_CONFIGURED":
        json_fh.write(f"# Invalid BLE address: '{addr}'\n")
        json_fh.write("# Please set MESHTASTIC_BLE_ADDR to your device's MAC address in run_badge.sh\n")
        json_fh.flush()
        connection_status = "No valid BLE address - check run_badge.sh"
        return
    
    addr = addr.strip()
    json_fh.write(f"# Using BLE address: '{addr}'\n")
    json_fh.flush()
    connection_status = f"Ready to connect to {addr}"
    
    retry_count = 0
    max_retries = 3  # Reduce retries, focus on making each attempt work
    
    while not stop_evt.is_set() and retry_count < max_retries:
        try:
            last_connection_attempt = time.time()
            connection_status = f"Connecting to {addr}... ({retry_count + 1}/{max_retries})"
            json_fh.write(f"# Connection attempt {retry_count + 1} to {addr}\n")
            json_fh.flush()
            
            link_up_evt.clear()
            
            # Create interface like the old working code
            json_fh.write(f"# Creating BLE interface for {addr}...\n")
            json_fh.flush()
            _iface = BLEInterface(address=addr, debugOut=json_fh)
            
            # Set up direct callback like old working code
            _iface.onReceive = direct_message_handler
            
            json_fh.write("# Interface created with direct callback\n")
            json_fh.flush()
            connection_status = "Interface created, connecting..."
            
            # Shorter wait - the old code connected quickly
            time.sleep(3)
            
            # Minimal test
            try:
                my_info = _iface.getMyNodeInfo()
                if my_info:
                    node_name = my_info.get('user', {}).get('longName', 'Unknown')
                    json_fh.write(f"# Connected to: {node_name}\n")
                    connection_status = f"Connected to {node_name}!"
                else:
                    json_fh.write("# Connected but no node info\n")
                    connection_status = "Connected"
                link_up_evt.set()
            except Exception as e:
                json_fh.write(f"# Test failed but assuming connected: {e}\n")
                connection_status = "Connected (test failed)"
                link_up_evt.set()  # Assume it works
            
            json_fh.flush()
            
            if link_up_evt.is_set():
                json_fh.write("# Entering keep-alive loop\n")
                json_fh.flush()
                retry_count = 0
                
                # Keep-alive loop inspired by old code's loop_forever
                last_keepalive = time.time()
                while not stop_evt.is_set():
                    current_time = time.time()
                    
                    # Handle outgoing messages
                    try:
                        msg = outgoing_q.get(timeout=0.1)  # Very short timeout
                        json_fh.write(f"# Sending: {msg}\n")
                        json_fh.flush()
                        connection_status = f"Sending: {msg[:20]}..."
                        
                        _iface.sendText(msg)
                        json_fh.write(f"# Message sent successfully\n")
                        json_fh.flush()
                        connection_status = "Message sent!"
                        last_keepalive = current_time
                        
                    except queue.Empty:
                        # No messages to send
                        pass
                    except Exception as e:
                        json_fh.write(f"# Send error: {e}\n")
                        json_fh.flush()
                        break
                    
                    # Minimal keepalive - much less aggressive than before
                    if current_time - last_keepalive > 120:  # 2 minutes
                        try:
                            # Just a simple ping
                            _iface.getMyNodeInfo()
                            last_keepalive = current_time
                            json_fh.write("# Keepalive successful\n")
                            json_fh.flush()
                        except Exception as e:
                            json_fh.write(f"# Keepalive failed: {e}\n")
                            json_fh.flush()
                            break
                    
                    # Update status if idle
                    if connection_status.endswith("(idle)"):
                        pass  # Don't spam
                    elif not connection_status.startswith("Sending") and not connection_status.endswith("sent!"):
                        connection_status = "Connected (idle)"
                    
                    # Brief sleep to prevent busy loop
                    time.sleep(0.5)
                
                # Connection lost
                link_up_evt.clear()
                json_fh.write("# Keep-alive loop exited\n")
                json_fh.flush()
                connection_status = "Connection lost"
                retry_count += 1
                
            else:
                retry_count += 1
                
        except Exception as e:
            json_fh.write(f"# Radio worker error: {e}\n")
            import traceback
            json_fh.write(f"# Traceback: {traceback.format_exc()}\n")
            json_fh.flush()
            retry_count += 1
            connection_status = f"Error: {str(e)[:50]}..."
            
        finally:
            link_up_evt.clear()
            if _iface:
                try:
                    _iface.close()
                    json_fh.write("# Interface closed\n")
                    json_fh.flush()
                except Exception as e:
                    json_fh.write(f"# Close error: {e}\n")
                    json_fh.flush()
                _iface = None
            
            if not stop_evt.is_set() and retry_count < max_retries:
                wait_time = 5 + retry_count * 3  # Shorter waits
                json_fh.write(f"# Retrying in {wait_time}s...\n")
                json_fh.flush()
                connection_status = f"Retrying in {wait_time}s..."
                stop_evt.wait(wait_time)
    
    if retry_count >= max_retries:
        json_fh.write(f"# Max retries exceeded for {addr}\n")
        json_fh.flush()
        connection_status = f"Max retries exceeded for {addr}"

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
