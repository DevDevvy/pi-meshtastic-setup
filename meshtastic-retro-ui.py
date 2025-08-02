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
_iface_lock = threading.Lock()
_iface      = None
connection_status = "Initializing..."  # For UI display
last_connection_attempt = 0

# ── SIMPLE MESSAGE HANDLER ──────────────────────────────────────────────────
def simple_message_handler(packet, interface=None, topic=pub.AUTO_TOPIC):
    # writeflush helper
    def log(line):
        json_fh.write(line + "\n")
        json_fh.flush()

    log(f"# PACKET: topic={topic} packet={packet}")
    try:
        txt_field = None

        # 1. packets delivered as dicts
        if isinstance(packet, dict):
            dec = packet.get("decoded", {})
            # easiest cases first
            txt_field = dec.get("text") or dec.get("data", {}).get("text")

            # new‑style: data.payload -> bytes list
            if txt_field is None and dec.get("data", {}).get("payload"):
                try:
                    pl = dec["data"]["payload"]
                    txt_field = (bytes(pl) if isinstance(pl, list) else pl).decode("utf‑8", "ignore")
                except Exception:
                    pass

            src = packet.get("fromId", "unknown")
            ts  = packet.get("rxTime", time.time())

        else:                                      # packet as object
            dec = getattr(packet, "decoded", None)
            if dec:
                txt_field = getattr(dec, "text", None)

                data = getattr(dec, "data", None)
                if txt_field is None and data:
                    txt_field = getattr(data, "text", None)
                    if txt_field is None and hasattr(data, "payload"):
                        try:
                            txt_field = bytes(data.payload).decode("utf‑8", "ignore")
                        except Exception:
                            pass

            src = getattr(packet, "fromId", "unknown")
            ts  = getattr(packet, "rxTime", time.time())
        # ---------- end extraction ----------

        if not txt_field:
            return   # not a text packet we care about

        if ts > 1e12:          # ms → s
            ts /= 1000
        text = txt_field[:MAX_LEN]

        log(f"# Received: {src}: {text}")
        with db:
            db.execute("INSERT INTO messages VALUES (?,?,?)", (ts, src, text))
        incoming_q.put((ts, src, text))
    except Exception as e:
        log(f"# Received: {src}: {text}")

def on_conn_established(interface=None, topic=pub.AUTO_TOPIC, **kwargs):
    link_up_evt.set()
    json_fh.write("# CONNECTION ESTABLISHED\n")

def on_conn_lost(interface=None, topic=pub.AUTO_TOPIC, **kwargs):
    link_up_evt.clear()
    json_fh.write("# CONNECTION LOST\n")

# ── PUBSUB SUBSCRIPTIONS ─────────────────────────────────────────────────────
pub.subscribe(simple_message_handler,        "meshtastic.receive")       # catches all receive.* events :contentReference[oaicite:0]{index=0}
pub.subscribe(on_conn_established,           "meshtastic.connection.established")
pub.subscribe(on_conn_lost,                  "meshtastic.connection.lost")

# ── RADIO THREAD ──────────────────────────────────────────────────────────────

def _radio_worker():
    global _iface, connection_status, last_connection_attempt
    
    # Use the explicit address provided
    addr = NODE_ADDR
    json_fh.write(f"# NODE_ADDR from env: '{addr}'\n")
    
    # Check if we have a valid address
    if not addr:
        json_fh.write(f"# Invalid BLE address: '{addr}'\n")
        json_fh.write("# Please set MESHTASTIC_BLE_ADDR to your device's MAC address in run_badge.sh\n")
        connection_status = "No valid BLE address - check run_badge.sh"
        return
    
    addr = addr.strip()  # Remove any whitespace
    json_fh.write(f"# Using BLE address: '{addr}'\n")
    
    # Skip the verification scan - go straight to connection attempt
    connection_status = f"Ready to connect to {addr}"
    
    retry_count = 0
    max_retries = 5
    
    while not stop_evt.is_set() and retry_count < max_retries:
        try:
            last_connection_attempt = time.time()
            connection_status = f"Connecting to {addr}... ({retry_count + 1}/{max_retries})"
            json_fh.write(f"# Connection attempt {retry_count + 1} to {addr}\n")
            
            # Clear previous state
            link_up_evt.clear()
            
            # Create interface with the explicit address
            json_fh.write(f"# Creating BLE interface for {addr}...\n")
            with _iface_lock:
                _iface = BLEInterface(address=addr, debugOut=json_fh)
            
            json_fh.write("# Interface created, testing connection...\n")
            connection_status = "Testing connection..."
            
            # Test connection by getting node info
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
                                # Use wantAck=True for reliable delivery
                                try:
                                    sent_packet = _iface.sendText(msg, wantAck=True)
                                    json_fh.write(f"# sendText returned: {sent_packet}\n")
                                    connection_status = "Message sent!"
                                except Exception as send_err:
                                    json_fh.write(f"# sendText error: {send_err}\n")
                                    connection_status = f"Send error: {send_err}"
                                    break
                                
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
        json_fh.write(f"# Max retries exceeded for {addr}\n")
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
    # Initialize curses
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.nodelay(True)                 # non-blocking getch()
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

    # Load history and start at bottom
    msgs = _history()
    h, w = stdscr.getmaxyx()
    pane_h = h - PAD_V*2 - 2
    viewofs = max(0, len(msgs) - pane_h)

    send_mode = False
    inp = ""
    TITLE = " Retro-Meshtastic Badge — Touch or ↑/↓ to scroll "

    while not stop_evt.is_set():
        # 1) Drain incoming queue
        try:
            while True:
                msgs.append(incoming_q.get_nowait())
        except queue.Empty:
            pass

        # 2) Auto-scroll to bottom
        h, w = stdscr.getmaxyx()
        pane_h = h - PAD_V*2 - 2
        viewofs = max(0, len(msgs) - pane_h)

        # 3) Draw the frame
        stdscr.erase()
        stdscr.addstr(0, 0, "╔" + TITLE.center(w-2, "═")[:w-2] + "╗", text_col)

        # Connection status
        if link_up_evt.is_set():
            safe_footer(stdscr, 1, "[● LINKED] Connected to " + NODE_ADDR, yes_link)
            row_start = PAD_V + 2
        else:
            safe_footer(stdscr, 1, "[○ NO LINK] " + connection_status[:w-5], no_link)
            if h > 10:
                debug = f"Last attempt: {int(time.time() - last_connection_attempt)}s ago"
                safe_footer(stdscr, 2, debug[:w-1], warn_col)
                row_start = PAD_V + 3
            else:
                row_start = PAD_V + 2

        # Message history
        row, used, idx = row_start, 0, viewofs
        while used < pane_h and idx < len(msgs):
            ts, src, txt = msgs[idx]
            # guard against None
            safe_src = (src or "unknown")[:10]
            prefix   = f"{_fmt(ts)} {safe_src:>10} │ "
            avail = w - len(prefix)
            for j, line in enumerate(textwrap.wrap(txt, width=avail) or [""]):
                if used >= pane_h:
                    break
                if j == 0:
                    line_out = (prefix + line).ljust(w)[:w]
                else:
                    line_out = (" " * len(prefix) + line).ljust(w)[:w]
                stdscr.addstr(row + used, 0, line_out, text_col)
                used += 1
            idx += 1

        stdscr.addstr(h-2, 0, "╚" + "═"*(w-2) + "╝", text_col)

        # Footer or send prompt
        if send_mode:
            prompt = f"Send> {inp}"
            safe_footer(stdscr, h-1, prompt, text_col)
            stdscr.move(h-1, min(len(prompt), w-2))
        else:
            footer = "[S]end  [Ctrl-C/Q] quit  ↑/↓ PgUp/PgDn  Touch scroll"
            safe_footer(stdscr, h-1, footer, text_col)

        stdscr.refresh()
        curses.napms(100)

        # 4) Handle input
        try:
            c = stdscr.getch()
        except curses.error:
            c = -1

        # Quit on Ctrl-C or Q
        if c in (3, ord('q'), ord('Q')):
            stop_evt.set()
            raise KeyboardInterrupt
        if stop_evt.is_set():
            return

        # Enter send mode
        if c in (ord('s'), ord('S')) and not send_mode:
            send_mode, inp = True, ""
            continue

        # In send mode: use Textbox
        if send_mode:
            curses.curs_set(1)
            h, w = stdscr.getmaxyx()
            prompt = "Send> "
            stdscr.addstr(h-1, 0, prompt, text_col)
            stdscr.clrtoeol()
            stdscr.refresh()

            win = curses.newwin(1, w - len(prompt) - 1, h-1, len(prompt))
            win.keypad(True)
            tb = curses.textpad.Textbox(win, insert_mode=True)

            def validator(ch):
                return 7 if ch in (10, 13) else ch

            try:
                s = tb.edit(validator).strip()
            finally:
                curses.curs_set(0)

            send_mode = False
            if s:
                ts = time.time()
                with db:
                    db.execute("INSERT INTO messages VALUES (?,?,?)", (ts, "You", s))
                msgs.append((ts, "You", s))
                outgoing_q.put(s)
            continue

        # Navigation
        if c == curses.KEY_UP:
            viewofs = max(0, viewofs - 1)
        elif c == curses.KEY_DOWN:
            viewofs = min(len(msgs) - pane_h, viewofs + 1)
        elif c == curses.KEY_PPAGE:
            viewofs = max(0, viewofs - pane_h)
        elif c == curses.KEY_NPAGE:
            viewofs = min(len(msgs) - pane_h, viewofs + pane_h)
        elif c == curses.KEY_MOUSE:
            _, mx, my, _, b = curses.getmouse()
            if b & curses.BUTTON4_PRESSED:
                viewofs = max(0, viewofs - 3)
            elif b & curses.BUTTON5_PRESSED:
                viewofs = min(len(msgs) - pane_h, viewofs + 3)

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
    stop_evt.set()
    json_fh.close()
    db.close()


if __name__ == "__main__":
    main()
