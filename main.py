#!/usr/bin/env python3
"""
Retro Meshtastic Badge – Robust 3.5″ Touch Console
 • Auto‑retry on /dev/rfcomm0 or auto‑scan fallback
 • PubSub‑driven link status & incoming packets
 • Single shared SerialInterface for send/receive
 • SQLite + JSON persistence
 • Curses UI with exact width & touch/scroll
"""

import os, json, sqlite3, queue, signal, threading, time
import curses
from pathlib import Path
from datetime import datetime

import meshtastic.serial_interface              # pip install meshtastic
from pubsub import pub                          # pip install PyPubSub

# ── CONFIG ───────────────────────────────────────────────────────────────────
DEV_PATH   = "/dev/rfcomm0"
DATA_DIR   = Path.home() / ".retrobadge"
DATA_DIR.mkdir(exist_ok=True)

LOG_FILE   = DATA_DIR / "meshtastic.log"
DB_FILE    = DATA_DIR / "meshtastic.db"

THEME_FG, THEME_BG = curses.COLOR_GREEN, curses.COLOR_BLACK
MAX_LEN, PAD_V    = 240, 2

# ── PERSISTENCE SETUP ─────────────────────────────────────────────────────────
json_fh = open(LOG_FILE, "a", encoding="utf‑8", buffering=1)
db      = sqlite3.connect(DB_FILE, check_same_thread=False)
with db:
    db.execute("""
      CREATE TABLE IF NOT EXISTS messages (
        ts   REAL,
        src  TEXT,
        txt  TEXT
      )
    """)

# ── THREAD‑SAFE STATE ──────────────────────────────────────────────────────────
incoming_q  = queue.Queue(1024)
link_up_evt = threading.Event()
stop_evt    = threading.Event()

# ── PUBSUB CALLBACKS ──────────────────────────────────────────────────────────
def on_text(packet, interface):
    """Handle incoming text packets."""
    decoded = getattr(packet, "decoded", {}) or packet.get("decoded", {})
    text    = decoded.get("text")
    if not text:
        return
    # timestamp normalization
    ts = getattr(packet, "rxTime", packet.get("timestamp", time.time()))
    if ts > 1e12:
        ts /= 1000
    src = getattr(packet, "fromId", packet.get("from", {}).get("userAlias", "unknown"))
    txt = text[:MAX_LEN]

    # persist
    json_fh.write(json.dumps(packet, default=str) + "\n")
    with db:
        db.execute("INSERT INTO messages VALUES (?,?,?)", (ts, src, txt))

    incoming_q.put((ts, src, txt))

def on_connected(interface):
    """Fired when link is up."""
    link_up_evt.set()

def on_disconnected(interface):
    """Fired when link is lost."""
    link_up_evt.clear()

# subscribe to Meshtastic PubSub topics :contentReference[oaicite:2]{index=2}
pub.subscribe(on_text,          'meshtastic.receive.text')
pub.subscribe(on_connected,     'meshtastic.connection.established')
pub.subscribe(on_disconnected,  'meshtastic.connection.lost')

# ── RADIO THREAD ──────────────────────────────────────────────────────────────
iface_lock = threading.Lock()
iface      = None

def radio_worker():
    global iface
    while not stop_evt.is_set():
        try:
            # first try explicit path, else auto‑scan
            try:
                new_iface = meshtastic.serial_interface.SerialInterface(devPath=DEV_PATH)
            except Exception:
                new_iface = meshtastic.serial_interface.SerialInterface()

            # share for sendText() calls
            with iface_lock:
                iface = new_iface

            # hang around until error or shutdown
            while not stop_evt.wait(0.5):
                pass

        except Exception as e:
            json_fh.write(f"# radio error: {e}\n")
            link_up_evt.clear()
            time.sleep(2)

        finally:
            with iface_lock:
                if iface:
                    try: iface.close()
                    except: pass
                    iface = None

# ── UTILITIES ─────────────────────────────────────────────────────────────────
def load_history(limit=2000):
    cur = db.cursor()
    cur.execute("SELECT ts, src, txt FROM messages ORDER BY ts DESC LIMIT ?", (limit,))
    return list(reversed(cur.fetchall()))

def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")

# ── CURSES UI ─────────────────────────────────────────────────────────────────
def run_ui(stdscr):
    curses.curs_set(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, THEME_FG, THEME_BG)
    color = curses.color_pair(1)

    msgs     = load_history()
    height, width = stdscr.getmaxyx()
    pad_h    = height - PAD_V*2
    view_ofs = max(0, len(msgs) - pad_h)

    send_mode = False
    input_buf = ""

    HEADER = "╔═ Retro‑Badge – Meshtastic ═╗"
    FOOTER = "[S]end  [Q]uit  ↑/↓ PgUp/PgDn Touch scroll"

    while not stop_evt.is_set():
        # consume incoming
        try:
            while True:
                msgs.append(incoming_q.get_nowait())
        except queue.Empty:
            pass

        height, width = stdscr.getmaxyx()
        pad_h = height - PAD_V*2

        # auto‑tail
        if view_ofs >= max(0, len(msgs) - pad_h):
            view_ofs = max(0, len(msgs) - pad_h)

        stdscr.erase()
        status = "[● LINK]" if link_up_evt.is_set() else "[○ NO LINK]"
        stdscr.addstr(0, 0, f"{HEADER} {status}".ljust(width)[:width], color)

        # draw messages
        for i in range(pad_h):
            idx = view_ofs + i
            if idx >= len(msgs):
                break
            ts, src, txt = msgs[idx]
            pre = f"{fmt_ts(ts)} {src[:10]:>10} │ "
            avail = width - len(pre)
            stdscr.addstr(PAD_V + i, 0, (pre + txt[:avail]).ljust(width)[:width], color)

        # footer / input
        if send_mode:
            prompt = "Send> " + input_buf
            stdscr.addstr(height-1, 0, prompt.ljust(width)[:width], color)
            stdscr.move(height-1, min(len(prompt), width-1))
        else:
            stdscr.addstr(height-1, 0, FOOTER.ljust(width)[:width], color)

        stdscr.refresh()
        curses.napms(30)

        # handle keys
        try:
            ch = stdscr.getch()
        except curses.error:
            continue

        if send_mode:
            if ch in (10, 13):
                text = input_buf.strip()
                send_mode = False
                input_buf = ""
                if text and link_up_evt.is_set():
                    ts = time.time()
                    with db:
                        db.execute("INSERT INTO messages VALUES (?,?,?)", (ts, "You", text))
                    msgs.append((ts, "You", text))
                    # thread‑safe send on the shared interface :contentReference[oaicite:3]{index=3}
                    with iface_lock:
                        try:
                            iface.sendText(text)
                        except Exception as e:
                            msgs.append((time.time(), "ERR", f"send failed: {e}"))
            elif ch in (27,):
                send_mode = False
            elif ch in (127, 8):
                input_buf = input_buf[:-1]
            elif 32 <= ch <= 126:
                input_buf += chr(ch)
            continue

        # navigation
        if ch == curses.KEY_UP:
            view_ofs = max(0, view_ofs - 1)
        elif ch == curses.KEY_DOWN:
            view_ofs = min(len(msgs)-pad_h, view_ofs + 1)
        elif ch == curses.KEY_PPAGE:
            view_ofs = max(0, view_ofs - pad_h)
        elif ch == curses.KEY_NPAGE:
            view_ofs = min(len(msgs)-pad_h, view_ofs + pad_h)
        elif ch == curses.KEY_MOUSE:
            _, mx, my, _, bstate = curses.getmouse()
            if bstate & curses.BUTTON4_PRESSED:
                view_ofs = max(0, view_ofs - 3)
            if bstate & curses.BUTTON5_PRESSED:
                view_ofs = min(len(msgs)-pad_h, view_ofs + 3)
            if bstate & curses.BUTTON1_PRESSED:
                start = my
            if bstate & curses.BUTTON1_RELEASED:
                delta = my - start
                view_ofs = max(0, min(len(msgs)-pad_h, view_ofs - delta))
        elif ch in (ord('s'), ord('S')):
            send_mode = True
            input_buf = ""
        elif ch in (ord('q'), ord('Q'), 27):
            stop_evt.set()

# ── ENTRY POINT ──────────────────────────────────────────────────────────────
def main():
    signal.signal(signal.SIGINT,  lambda *_: stop_evt.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_evt.set())

    # start radio ↔ mesh I/O
    rt = threading.Thread(target=radio_worker, daemon=True)
    rt.start()

    # launch UI
    curses.wrapper(run_ui)

    # cleanup
    stop_evt.set()
    rt.join(1)
    json_fh.close()
    db.close()

if __name__ == "__main__":
    main()
