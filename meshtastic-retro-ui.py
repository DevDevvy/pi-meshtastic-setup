#!/usr/bin/env python3
"""
meshtastic-retro-ui.py

Retro Meshtastic Badge – 3.5″ Touch, Headless Edition
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
from pubsub import pub

# ── CONFIG ───────────────────────────────────────────────────────────────────
DATA_DIR = Path.home() / ".retrobadge"; DATA_DIR.mkdir(exist_ok=True)
DB_FILE  = DATA_DIR / "meshtastic.db"
LOG_FILE = DATA_DIR / "meshtastic.log"
NODE_ADDR = os.getenv("MESHTASTIC_BLE_ADDR", None)  # e.g. "48:CA:43:3C:51:FD"

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

# ── PubSub callbacks ─────────────────────────────────────────────────────────
def _handle_text(pkt):
    """pkt is a dict from meshtastic.receive.text"""
    ts  = getattr(pkt, "rxTime", pkt.get("timestamp", time.time()))
    if ts > 1e12: ts /= 1000
    src = getattr(pkt, "fromId", pkt.get("from", {}).get("userAlias", "unknown"))
    txt = (pkt.decoded.text if hasattr(pkt, "decoded") else pkt["decoded"]["text"])[:MAX_LEN]
    json_fh.write(json.dumps(pkt, default=str) + "\n")
    with db:
        db.execute("INSERT INTO messages VALUES (?,?,?)", (ts, src, txt))
    incoming_q.put((ts, src, txt))

# subscribe with matching signatures:
pub.subscribe(_handle_text,                      "meshtastic.receive.text")
pub.subscribe(lambda: link_up_evt.set(),         "meshtastic.connection.established")
pub.subscribe(lambda: link_up_evt.clear(),       "meshtastic.connection.lost")

# ── RADIO THREAD ──────────────────────────────────────────────────────────────
def _find_ble_node():
    json_fh.write("# Scanning for BLE devices…\n")
    devices = BLEInterface.scan()  # filters Meshtastic UUID 
    json_fh.write(f"# Found {len(devices)} BLE devices.\n")
    for d in devices:
        json_fh.write(f"#   • {d.name} @ {d.address}\n")
    return devices[0].address if devices else None

def _radio_worker():
    global _iface
    addr = NODE_ADDR or _find_ble_node()
    while not stop_evt.is_set():
        try:
            json_fh.write(f"# Attempting BLE connection to: {addr}\n")
            iface = BLEInterface(address=addr, debugOut=json_fh)
            iface.waitForConfig()  # blocks until node DB downloaded 
            with _iface_lock:
                _iface = iface

            json_fh.write("# BLE link is up, entering main loop.\n")
            while not stop_evt.wait(0.1):
                try:
                    msg = outgoing_q.get_nowait()
                    iface.sendText(msg)
                except queue.Empty:
                    pass

        except Exception as e:
            json_fh.write(f"# Radio error: {e}\n")
            link_up_evt.clear()
            with _iface_lock:
                if _iface:
                    try: _iface.close()
                    except: pass
                    _iface = None

            new_addr = _find_ble_node()
            if new_addr and new_addr != addr:
                addr = new_addr
                json_fh.write(f"# Switching to BLE address: {addr}\n")
            time.sleep(5)

# ── HELPERS ─────────────────────────────────────────────────────────────────
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
    curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)  # text
    curses.init_pair(2, curses.COLOR_RED,   curses.COLOR_BLACK)  # NO LINK
    curses.init_pair(3, curses.COLOR_BLUE,  curses.COLOR_BLACK)  # LINK

    text_col = curses.color_pair(1)
    no_link  = curses.color_pair(2)
    yes_link = curses.color_pair(3)

    msgs    = _history()
    viewofs = max(0, len(msgs) - (curses.LINES - PAD_V*2 - 2))
    send_mode = False
    inp = ""

    TITLE = " Retro-Meshtastic Badge — Touch or ↑/↓ to scroll "

    while not stop_evt.is_set():
        # Drain new messages
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

        # Render wrapped history
        row = PAD_V + 2
        used = 0
        idx = viewofs
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
            if c in (10, 13):
                msg = inp.strip()
                send_mode = False
                inp = ""
                if msg:
                    ts = time.time()
                    with db:
                        db.execute("INSERT INTO messages VALUES (?,?,?)", (ts, "You", msg))
                    msgs.append((ts, "You", msg))
                    outgoing_q.put(msg)
            elif c in (27,):
                send_mode = False
            elif c in (127, 8):
                inp = inp[:-1]
            elif 32 <= c <= 126:
                inp += chr(c)
            continue

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
            send_mode = True; inp = ""
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
