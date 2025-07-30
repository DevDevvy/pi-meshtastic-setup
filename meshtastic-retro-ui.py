#!/usr/bin/env python3
"""
Retro Meshtastic Badge – 3.5″ Touch Console
 • Persistent SerialInterface on /dev/rfcomm0
 • Auto‑retry on disconnect
 • Exact-width curses UI with touch/scroll
 • JSON + SQLite persistence
"""

import os, json, sqlite3, queue, signal, threading, time
import curses
from pathlib import Path
from datetime import datetime

import meshtastic.serial_interface as mserial  # pip install meshtastic

# ── CONFIG ────────────────────────────────────────────────────────────────────
DEV_PATH   = os.getenv("MESHTASTIC_DEV", "/dev/rfcomm0")
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
    db.execute("""CREATE TABLE IF NOT EXISTS messages (
                      ts   REAL,
                      src  TEXT,
                      txt  TEXT
                  )""")

# ── THREAD‑SAFE QUEUES & FLAGS ────────────────────────────────────────────────
incoming_q  = queue.Queue(1024)
outgoing_q  = queue.Queue(256)
link_up_evt = threading.Event()
stop_evt    = threading.Event()

# ── RADIO THREAD ──────────────────────────────────────────────────────────────
def radio_thread():
    iface = None
    while not stop_evt.is_set():
        try:
            link_up_evt.clear()
            iface = mserial.SerialInterface(devPath=DEV_PATH)
            # on_receive callback for any packet
            def on_receive(pkt, _if):
                json_fh.write(json.dumps(pkt, default=str) + "\n")
                decoded = getattr(pkt, "decoded", {}) or pkt.get("decoded", {})
                text    = getattr(decoded, "text", None) or decoded.get("text")
                if not text:
                    return
                src = getattr(pkt, "fromId", pkt.get("from", {}).get("userAlias", "unknown"))
                ts  = getattr(pkt, "rxTime", pkt.get("timestamp", time.time())/1000)
                text = text[:MAX_LEN]
                with db:
                    db.execute("INSERT INTO messages VALUES (?,?,?)", (ts, src, text))
                incoming_q.put((ts, src, text))
            iface.onReceive = on_receive

            link_up_evt.set()
            # dispatch outgoing messages
            while not stop_evt.is_set():
                try:
                    msg = outgoing_q.get(timeout=0.2)
                    iface.sendText(msg)  # :contentReference[oaicite:4]{index=4}
                except queue.Empty:
                    pass

        except Exception as e:
            json_fh.write(f"# radio error: {e}\n")
            time.sleep(2)
        finally:
            if iface:
                try: iface.close()
                except: pass

# ── UI & INPUT ────────────────────────────────────────────────────────────────
def load_history(limit=2000):
    cur = db.cursor()
    cur.execute("SELECT ts, src, txt FROM messages ORDER BY ts DESC LIMIT ?", (limit,))
    return list(reversed(cur.fetchall()))

def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")

def run_ui(stdscr):
    curses.curs_set(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, THEME_FG, THEME_BG)
    color = curses.color_pair(1)

    msgs    = load_history()
    view_ofs = max(0, len(msgs) - (curses.LINES - PAD_V*2))
    send_mode, buf = False, ""

    HEADER = "╔═ Retro‑Badge – Meshtastic ═╗"
    FOOTER = "[S]end  [Q]uit  ↑/↓  PgUp/PgDn  Touch scroll"

    while not stop_evt.is_set():
        # collect incoming
        try:
            while True:
                msgs.append(incoming_q.get_nowait())
        except queue.Empty:
            pass

        h, w   = stdscr.getmaxyx()
        pad_h = h - PAD_V*2

        # auto‑tail
        if view_ofs >= max(0, len(msgs) - pad_h):
            view_ofs = max(0, len(msgs) - pad_h)

        stdscr.erase()
        link_txt = "[● LINK]" if link_up_evt.is_set() else "[○ NO LINK]"
        try:
            stdscr.addstr(0, 0, f"{HEADER} {link_txt}".ljust(w)[:w], color)
        except curses.error:
            pass
        # messages
        for i in range(pad_h):
            idx = view_ofs + i
            if idx >= len(msgs):
                break
            ts, src, txt = msgs[idx]
            prefix = f"{fmt_ts(ts)} {src[:10]:>10} │ "
            avail  = w - len(prefix)
            try:
                stdscr.addstr(PAD_V + i, 0, (prefix + txt[:avail]).ljust(w)[:w], color)
            except curses.error:
                pass
        # input / footer
        if send_mode:
            prompt = "Send> " + buf
            try:
                stdscr.addstr(h - 1, 0, prompt.ljust(w)[:w], color)
                stdscr.move(h - 1, min(len(prompt), w - 1))
            except curses.error:
                pass
        else:
            try:
                stdscr.addstr(h - 1, 0, FOOTER.ljust(w)[:w], color)
            except curses.error:
                pass
        stdscr.refresh()
        curses.napms(30)

        # key handling
        try:
            c = stdscr.getch()
        except curses.error:
            continue

        if send_mode:
            if c in (10,13):
                msg = buf.strip()
                buf, send_mode = "", False
                if msg and link_up_evt.is_set():
                    ts = time.time()
                    with db:
                        db.execute("INSERT INTO messages VALUES (?,?,?)", (ts, "You", msg))
                    msgs.append((ts, "You", msg))
                    outgoing_q.put(msg)
            elif c in (27,):
                send_mode = False
            elif c in (127,8):
                buf = buf[:-1]
            elif 32 <= c <= 126:
                buf += chr(c)
            continue

        # navigation
        if c == curses.KEY_UP:
            view_ofs = max(0, view_ofs - 1)
        elif c == curses.KEY_DOWN:
            view_ofs = min(len(msgs)-pad_h, view_ofs + 1)
        elif c == curses.KEY_PPAGE:
            view_ofs = max(0, view_ofs - pad_h)
        elif c == curses.KEY_NPAGE:
            view_ofs = min(len(msgs)-pad_h, view_ofs + pad_h)
        elif c == curses.KEY_MOUSE:
            _, mx, my, _, b = curses.getmouse()
            if b & curses.BUTTON4_PRESSED:
                view_ofs = max(0, view_ofs - 3)
            if b & curses.BUTTON5_PRESSED:
                view_ofs = min(len(msgs)-pad_h, view_ofs + 3)
            if b & curses.BUTTON1_PRESSED:
                view_ofs = max(0, view_ofs - (my - PAD_V))
            if b & curses.BUTTON1_RELEASED:
                view_ofs = min(len(msgs)-pad_h, view_ofs + (my - PAD_V))
        elif c in (ord('s'), ord('S')):
            send_mode, buf = True, ""
        elif c in (ord('q'), ord('Q'), 27):
            stop_evt.set()

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    signal.signal(signal.SIGINT,  lambda *_: stop_evt.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_evt.set())

    rt = threading.Thread(target=radio_thread, daemon=True)
    rt.start()

    curses.wrapper(run_ui)

    stop_evt.set()
    rt.join(2)
    json_fh.close()
    db.close()

if __name__ == "__main__":
    main()
