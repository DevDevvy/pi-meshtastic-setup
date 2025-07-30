#!/usr/bin/env python3
"""
Retro Meshtastic Badge – 3.5″ touch console
 • Single, persistent SerialInterface
 • True send/receive on the mesh
 • Exact text‑width truncation
 • Scroll with finger, wheel, ↑/↓, PgUp/PgDn
 • SQLite + JSON logging
"""

import curses, json, os, queue, signal, sqlite3, threading, time
from pathlib import Path
from datetime import datetime

import meshtastic.serial_interface as mserial  # pip install meshtastic

# ── CONFIG ───────────────────────────────────────────────────────────────────
DEV_PATH = os.getenv("MESHTASTIC_DEV", "/dev/rfcomm0")
DATA_DIR = Path.home() / ".retrobadge"
DATA_DIR.mkdir(exist_ok=True)

LOG_FILE = DATA_DIR / "meshtastic.log"
DB_FILE  = DATA_DIR / "meshtastic.db"

THEME_FG, THEME_BG = curses.COLOR_GREEN, curses.COLOR_BLACK
MAX_LEN, PAD_V = 240, 2

# ── PERSISTENCE ───────────────────────────────────────────────────────────────
json_fh = open(LOG_FILE, "a", encoding="utf‑8", buffering=1)
db      = sqlite3.connect(DB_FILE, check_same_thread=False)
with db:
    db.execute("""CREATE TABLE IF NOT EXISTS messages (
                      ts   REAL,
                      src  TEXT,
                      txt  TEXT
                  )""")

# ── THREAD‑SAFE QUEUES & STATE ────────────────────────────────────────────────
incoming_q = queue.Queue(1024)
stop_evt   = threading.Event()

# ── MESHTASTIC CALLBACK ──────────────────────────────────────────────────────
def on_receive(pkt, iface):
    """Parse any pkt, log JSON+SQLite, queue for UI."""
    try:
        json_fh.write(json.dumps(pkt, default=str) + "\n")
        text, src, ts = None, None, None

        # v2.3+ proto style
        if hasattr(pkt, "decoded") and getattr(pkt.decoded, "text", None):
            text = pkt.decoded.text; src = str(pkt.fromId); ts = pkt.rxTime
        # legacy dict style
        elif isinstance(pkt, dict):
            text = pkt.get("decoded", {}).get("text")
            src  = pkt.get("from", {}).get("userAlias") or str(pkt.get("from"))
            ts   = pkt.get("timestamp", time.time()) / (1 if pkt.get("timestamp",0)<1e12 else 1000)

        if not text:
            return
        text = text[:MAX_LEN]

        with db:
            db.execute("INSERT INTO messages VALUES (?,?,?)", (ts, src, text))
        incoming_q.put_nowait((ts, src, text))
    except Exception as e:
        json_fh.write(f"# callback error: {e}\n")

# ── LOAD PAST MESSAGES ────────────────────────────────────────────────────────
def load_history(limit=2000):
    cur = db.cursor()
    cur.execute("SELECT ts, src, txt FROM messages ORDER BY ts DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    return list(reversed(rows))

def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")

# ── CURSES UI ────────────────────────────────────────────────────────────────
def ui(stdscr, iface):
    curses.curs_set(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, THEME_FG, THEME_BG)
    color = curses.color_pair(1)

    msgs = load_history()
    view_ofs = max(0, len(msgs) - (curses.LINES - PAD_V*2))
    send_mode, inp = False, ""

    HEADER = "╔═ Retro‑Badge – Meshtastic ═╗"
    FOOTER = "[S]end  [Q]uit  ↑/↓/PgUp/PgDn  Touch scroll"

    while not stop_evt.is_set():
        # drain new packets
        try:
            while True:
                msgs.append(incoming_q.get_nowait())
        except queue.Empty:
            pass

        h, w = stdscr.getmaxyx()
        pad_h = h - PAD_V*2

        # auto‑tail
        if view_ofs >= max(0, len(msgs) - pad_h):
            view_ofs = max(0, len(msgs) - pad_h)

        stdscr.erase()
        # header
        link_txt = "[● LINK]" if iface else "[○ NO LINK]"
        stdscr.addstr(0, 0, f"{HEADER} {link_txt}".ljust(w)[:w], color)

        # message pane
        for i in range(pad_h):
            idx = view_ofs + i
            if idx >= len(msgs): break
            ts, src, txt = msgs[idx]
            prefix = f"{fmt_ts(ts)} {src[:10]:>10} │ "
            avail  = w - len(prefix)
            stdscr.addstr(PAD_V + i, 0, (prefix + txt[:avail]).ljust(w)[:w], color)

        # footer / send prompt
        if send_mode:
            prompt = "Send> " + inp
            stdscr.addstr(h - 1, 0, prompt.ljust(w)[:w], color)
            stdscr.move(h - 1, min(len(prompt), w - 1))
        else:
            stdscr.addstr(h - 1, 0, FOOTER.ljust(w)[:w], color)

        stdscr.refresh()
        curses.napms(30)

        # input
        try:
            c = stdscr.getch()
        except curses.error:
            continue

        if send_mode:
            if c in (10, 13):
                msg = inp.strip()
                inp, send_mode = "", False
                if msg and iface:
                    ts = time.time()
                    with db:
                        db.execute("INSERT INTO messages VALUES (?,?,?)", (ts, "You", msg))
                    msgs.append((ts, "You", msg))
                    iface.sendText(msg)  # :contentReference[oaicite:0]{index=0}
            elif c in (27,):        send_mode = False
            elif c in (127, 8):     inp = inp[:-1]
            elif 32 <= c <= 126:    inp += chr(c)
            continue

        # navigation
        if c == curses.KEY_UP:      view_ofs = max(0, view_ofs - 1)
        elif c == curses.KEY_DOWN:  view_ofs = min(len(msgs)-pad_h, view_ofs + 1)
        elif c == curses.KEY_PPAGE: view_ofs = max(0, view_ofs - pad_h)
        elif c == curses.KEY_NPAGE: view_ofs = min(len(msgs)-pad_h, view_ofs + pad_h)
        elif c == curses.KEY_MOUSE:
            _, mx, my, _, b = curses.getmouse()
            if b & curses.BUTTON4_PRESSED: view_ofs = max(0, view_ofs - 3)
            if b & curses.BUTTON5_PRESSED: view_ofs = min(len(msgs)-pad_h, view_ofs + 3)
            # simple tap‑scroll: top half = up, bottom half = down
            if b & curses.BUTTON1_PRESSED:
                view_ofs = max(0, view_ofs - (my - PAD_V))
            if b & curses.BUTTON1_RELEASED:
                view_ofs = min(len(msgs)-pad_h, view_ofs + (my - PAD_V))
        elif c in (ord('s'), ord('S')):
            send_mode, inp = True, ""
        elif c in (ord('q'), ord('Q'), 27):
            stop_evt.set()

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    # try to open SerialInterface once
    try:
        iface = mserial.SerialInterface(devPath=DEV_PATH)
        iface.onReceive = on_receive
    except Exception as e:
        print(f"⚠️ Could not open {DEV_PATH!r}: {e}")
        iface = None

    # launch curses UI
    try:
        curses.wrapper(ui, iface)
    finally:
        stop_evt.set()
        if iface:
            iface.close()
        db.close()
        json_fh.close()

if __name__ == "__main__":
    signal.signal(signal.SIGINT,  lambda *_: stop_evt.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_evt.set())
    main()
