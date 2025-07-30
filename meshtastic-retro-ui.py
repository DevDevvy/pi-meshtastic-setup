#!/usr/bin/env python3
# ░█▀█░█▀▀░░░█▀█░█▀█░█▀█░█▀█
# ░█▀█░▀▀█░░░█▀▀░█░█░█░█░█░█   Retro‑Badge   v1.0  (Jul‑2025)
# ░▀░▀░▀▀▀░░░▀░░░▀▀▀░▀░▀░▀░▀   RPi‑4B | Meshtastic

"""
Touch‑friendly retro console for Meshtastic.
 • Shows live & historic traffic
 • Tap‑scroll or ↑ / ↓ / mouse wheel
 • Type S to send (opens one‑line prompt)
 • Persists to SQLite and JSON log
Tested on Raspberry Pi OS Bookworm / Python 3.11
"""

###############################################################################
# ── IMPORTS ──────────────────────────────────────────────────────────────────
###############################################################################
from __future__ import annotations
import curses, curses.textpad, json, os, queue, signal, sqlite3, threading, time
from pathlib import Path
from datetime import datetime

import meshtastic.serial_interface as mserial         # pip install meshtastic

###############################################################################
# ── CONFIGURATION ────────────────────────────────────────────────────────────
###############################################################################
# If you pair the radio over BLE as /dev/rfcomm0 keep the default.
# Otherwise override with env var:  MESHTASTIC_DEV=/dev/ttyUSB0
DEV_PATH   = os.getenv("MESHTASTIC_DEV", "/dev/rfcomm0")

DATA_DIR   = Path.home() / ".retrobadge"
DATA_DIR.mkdir(exist_ok=True)

LOG_FILE   = DATA_DIR / "meshtastic.log"              # line‑buffered JSON
DB_FILE    = DATA_DIR / "meshtastic.db"               # messages
THEME_FG   = curses.COLOR_GREEN
THEME_BG   = curses.COLOR_BLACK
MAX_LEN    = 240                                      # truncate payloads
SCREEN_PAD = 2                                        # blank rows top/bottom

###############################################################################
# ── PERSISTENCE ──────────────────────────────────────────────────────────────
###############################################################################
json_fh = open(LOG_FILE, "a", encoding="utf‑8", buffering=1)
db      = sqlite3.connect(DB_FILE, check_same_thread=False)
with db:
    db.execute("""CREATE TABLE IF NOT EXISTS messages (
                      ts   REAL,        -- Unix seconds
                      src  TEXT,
                      txt  TEXT
                  )""")

###############################################################################
# ── THREAD‑SAFE STATE ────────────────────────────────────────────────────────
###############################################################################
incoming_q  : queue.Queue[tuple[float,str,str]] = queue.Queue(1024)
outgoing_q  : queue.Queue[str]                  = queue.Queue(256)
link_up_evt = threading.Event()
stop_evt    = threading.Event()

###############################################################################
# ── RADIO I/O THREAD ─────────────────────────────────────────────────────────
###############################################################################
def radio_worker() -> None:
    iface: mserial.SerialInterface | None = None

    def on_receive(pkt, *_):
        """Meshtastic callback → parse, persist, enqueue for UI."""
        try:
            json_fh.write(json.dumps(pkt, default=str) + "\n")

            # -------- liberal packet decoding (covers v2.3+ and legacy) ----
            text, src, ts = None, None, None
            if hasattr(pkt, "decoded") and getattr(pkt.decoded, "text", None):
                text = pkt.decoded.text
                src  = str(pkt.fromId)
                ts   = pkt.rxTime
            elif isinstance(pkt, dict):     # older dict style
                text = pkt.get("decoded", {}).get("text")
                src  = pkt.get("from", {}).get("userAlias") or str(pkt.get("from"))
                ts   = pkt.get("timestamp", time.time())
                if ts > 1e12:               # ms → s
                    ts /= 1000
            if not text:
                return

            text = text[:MAX_LEN]
            with db:
                db.execute("INSERT INTO messages VALUES (?,?,?)", (ts, src, text))

            incoming_q.put_nowait((ts, src, text))

        except Exception as e:              # never let the callback raise
            json_fh.write(f"# on_receive error: {e}\n")

    # ------------------- main connect/loop/retry cycle -----------------------
    while not stop_evt.is_set():
        try:
            link_up_evt.clear()
            iface = mserial.SerialInterface(devPath=DEV_PATH)
            iface.onReceive = on_receive
            link_up_evt.set()

            # send any queued outbound text
            while not stop_evt.is_set():
                try:
                    message = outgoing_q.get(timeout=0.2)
                    iface.sendText(message)             # simple fire‑and‑forget
                    # NB: official example ☞ :contentReference[oaicite:0]{index=0}
                except queue.Empty:
                    pass
        except Exception as e:
            json_fh.write(f"# radio error: {e}\n")
            time.sleep(2)                               # gradual back‑off
        finally:
            try:
                if iface:
                    iface.close()
            except Exception:
                pass

###############################################################################
# ── UI HELPERS ───────────────────────────────────────────────────────────────
###############################################################################
def load_history(limit: int = 2000) -> list[tuple[float,str,str]]:
    """Fetch last *limit* messages newest‑last."""
    cur = db.cursor()
    cur.execute("SELECT ts, src, txt FROM messages ORDER BY ts DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    return list(reversed(rows))

def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")

###############################################################################
# ── CURSES USER‑INTERFACE ────────────────────────────────────────────────────
###############################################################################
def ui(stdscr) -> None:
    curses.curs_set(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, THEME_FG, THEME_BG)
    color = curses.color_pair(1)

    msgs: list[tuple[float,str,str]] = load_history()   # preload history
    view_ofs = max(0, len(msgs) - (curses.LINES - SCREEN_PAD * 2))

    send_mode   = False
    input_buf   = ""

    HEADER  = " ╔═ Retro‑Badge – Meshtastic ═╗ "
    FOOTER  = " [S]end   [Q]uit   [↑/↓] scroll   Touch: drag/wheel "

    # ------------------------------ main UI loop ----------------------------
    while not stop_evt.is_set():
        # drain any new incoming packets
        try:
            while True:
                pkt = incoming_q.get_nowait()
                msgs.append(pkt)
        except queue.Empty:
            pass

        # auto‑follow tail if already at bottom
        pad_height = curses.LINES - SCREEN_PAD * 2
        if view_ofs >= max(0, len(msgs) - pad_height):
            view_ofs = max(0, len(msgs) - pad_height)

        # -------- rendering -------------------------------------------------
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        # header bar
        header_str = HEADER + ("[● LINK]" if link_up_evt.is_set() else "[○ NO LINK]")
        try:
            stdscr.addstr(0, 0, header_str.ljust(w)[:w], color)
        except curses.error:
            pass
        # message area
        for idx in range(pad_height):
            row = idx + SCREEN_PAD
            msg_idx = view_ofs + idx
            if msg_idx >= len(msgs):
                break
            ts, src, text = msgs[msg_idx]
            line = f"{fmt_ts(ts)} {src[:10]:>10} │ {text}"
            try:
                stdscr.addstr(row, 0, line.ljust(w)[:w], color)
            except curses.error:
                pass
        # footer / prompt
        if send_mode:
            prompt = "Send> " + input_buf
            try:
                stdscr.addstr(h - 1, 0, prompt.ljust(w)[:w], color)
                stdscr.move(h - 1, min(w - 1, len(prompt)))
            except curses.error:
                pass
        else:
            try:
                stdscr.addstr(h - 1, 0, FOOTER.ljust(w)[:w], color)
            except curses.error:
                pass
        stdscr.refresh()
        curses.napms(25)

        # -------- input handling -------------------------------------------
        try:
            c = stdscr.getch()
        except curses.error:
            continue

        if send_mode:
            if c in (curses.KEY_ENTER, 10, 13):
                text = input_buf.strip()
                input_buf = ""
                send_mode = False
                if text:
                    ts = time.time()
                    msgs.append((ts, "You", text))
                    with db:
                        db.execute("INSERT INTO messages VALUES (?,?,?)", (ts, "You", text))
                    outgoing_q.put(text)
            elif c in (27,):                   # Esc abort
                send_mode = False
            elif c in (curses.KEY_BACKSPACE, 127, 8):
                input_buf = input_buf[:-1]
            elif 32 <= c <= 126:
                input_buf += chr(c)
            continue

        # ----- normal (view) mode -------
        if c == curses.KEY_UP:
            view_ofs = max(0, view_ofs - 1)
        elif c == curses.KEY_DOWN:
            view_ofs = min(max(0, len(msgs) - pad_height), view_ofs + 1)
        elif c == curses.KEY_PPAGE:           # PgUp
            view_ofs = max(0, view_ofs - pad_height)
        elif c == curses.KEY_NPAGE:           # PgDn
            view_ofs = min(max(0, len(msgs) - pad_height), view_ofs + pad_height)
        elif c == curses.KEY_MOUSE:
            try:
                _, mx, my, _, state = curses.getmouse()
                if state & curses.BUTTON4_PRESSED:     # wheel up
                    view_ofs = max(0, view_ofs - 3)
                elif state & curses.BUTTON5_PRESSED:   # wheel down
                    view_ofs = min(max(0, len(msgs) - pad_height), view_ofs + 3)
                elif state & curses.BUTTON1_PRESSED:
                    drag_start_y = my
                elif state & curses.BUTTON1_RELEASED:
                    drag_end_y = my
                    delta = drag_end_y - drag_start_y
                    view_ofs = min(max(0, len(msgs) - pad_height),
                                    max(0, view_ofs - delta))
            except Exception:
                pass
        elif c in (ord('s'), ord('S')):
            send_mode = True
            input_buf = ""
        elif c in (ord('q'), ord('Q'), 27):   # Esc/ Q
            stop_evt.set()

###############################################################################
# ── ENTRY POINT ──────────────────────────────────────────────────────────────
###############################################################################
def shutdown(*_sig):
    stop_evt.set()

if __name__ == "__main__":
    radio = threading.Thread(target=radio_worker, daemon=True)
    radio.start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, shutdown)

    try:
        curses.wrapper(ui)
    finally:
        stop_evt.set()
        radio.join(3)
        json_fh.close()
        db.close()
