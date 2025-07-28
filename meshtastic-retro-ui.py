#!/usr/bin/env python3
"""
Retro Meshtastic badge – 3.5‑inch console/touch version
Author: <you>
Raspberry Pi OS bookworm / Python 3.11
"""

import curses, json, sqlite3, threading, time, os, queue, signal
from pathlib import Path
from datetime import datetime
import meshtastic.serial_interface as mserial          # pip install meshtastic

# ── CONFIG ────────────────────────────────────────────────────────────────────
DEV_PATH   = "/dev/rfcomm0"                 # created by your BLE‑pair script
LOG_FILE   = Path("/home/pi/meshtastic.log")
DB_FILE    = Path("/home/pi/meshtastic.db")
COLOR_FG   = curses.COLOR_GREEN
COLOR_BG   = curses.COLOR_BLACK
MAX_MSGLEN = 240                            # truncate very long payloads

# ── PERSISTENCE SET‑UP ────────────────────────────────────────────────────────
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
DB_FILE.parent.mkdir(parents=True,  exist_ok=True)

json_fh = open(LOG_FILE, "a", encoding="utf‑8", buffering=1)     # line‑buffered
db      = sqlite3.connect(DB_FILE, check_same_thread=False)
with db:
    db.execute("""CREATE TABLE IF NOT EXISTS messages (
                      ts  REAL,          -- unix, seconds
                      src TEXT,
                      txt TEXT
                  )""")

# ── SHARED STATE ──────────────────────────────────────────────────────────────
messages      : list[tuple[str,str]] = []   # (src, text)   – newest last
msg_q         : queue.Queue       = queue.Queue(maxsize=512)  # from radio→UI
state_lock    = threading.Lock()
connection_ok = threading.Event()           # set when SerialInterface is live
stop_event    = threading.Event()

# ── RADIO CALLBACK ────────────────────────────────────────────────────────────
def on_receive(pkt, *_):
    """
    Robustly pull text out of any variant of Meshtastic packet,
    log raw JSON and decoded text, then hand to UI via queue.
    """
    try:
        json_fh.write(json.dumps(pkt, default=str) + "\n")

        # —— extract decoded text ——
        txt = None
        #  v2.3+  python‑proto objects
        if hasattr(pkt, "decoded") and hasattr(pkt.decoded, "text"):
            txt = pkt.decoded.text
            src = str(pkt.fromId)
            ts  = pkt.rxTime
        #  Dict style (meshtastic.util.streamInterface)
        elif isinstance(pkt, dict):
            txt = pkt.get("decoded", {}).get("text")
            src = pkt.get("from", {}).get("userAlias", str(pkt.get("from")))
            ts  = pkt.get("timestamp", time.time())
            if ts > 1e12:                       # ms → s
                ts /= 1000
        else:
            return                              # nothing interesting

        if not txt:
            return
        txt = txt[:MAX_MSGLEN]

        # —— log to SQLite ——
        with db:
            db.execute("INSERT INTO messages VALUES (?,?,?)",
                       (ts, src, txt))

        # —— push to UI ——
        msg_q.put_nowait((src[:10], txt))

    except Exception as e:                      # never raise inside callback
        json_fh.write(f"# callback error: {e}\n")

# ── RADIO THREAD ──────────────────────────────────────────────────────────────
def radio_thread():
    iface = None
    while not stop_event.is_set():
        try:
            connection_ok.clear()
            iface = mserial.SerialInterface(devPath=DEV_PATH)
            iface.onReceive = on_receive
            connection_ok.set()                 # success!
            while not stop_event.is_set():
                time.sleep(0.5)                 # stay alive
        except Exception as e:
            json_fh.write(f"# serial err: {e}\n")
            time.sleep(2)                       # back‑off
        finally:
            try:                               # cleanup if needed
                if iface:
                    iface.close()
            except Exception:
                pass

# ── LAUNCH RADIO ──────────────────────────────────────────────────────────────
rt = threading.Thread(target=radio_thread, daemon=True)
rt.start()

# ── CURSES UI ────────────────────────────────────────────────────────────────
def run_ui(stdscr):
    # console initialisation ---------------------------------------------------
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, COLOR_FG, COLOR_BG)

    offset = 0             # first visible message index
    sending = False        # True while user is typing a message
    input_buf = ""

    HEADER = " Retro Meshtastic – touch drag or ↑/↓ to scroll "
    FOOTER = " [S]end  [Q]uit "

    while not stop_event.is_set():
        # fetch any newly‑arrived messages ------------------------------
        try:
            while True:
                src, txt = msg_q.get_nowait()
                messages.append((src, txt))
        except queue.Empty:
            pass

        # size & layout --------------------------------------------------
        h, w = stdscr.getmaxyx()
        pane_height = h - 4                           # header + footer + border
        max_offset  = max(0, len(messages) - pane_height)

        # auto‑scroll if at bottom --------------------------------------
        if offset == max_offset:
            offset = max_offset                      # follow new msgs

        # drawing --------------------------------------------------------
        stdscr.erase()
        colour = curses.color_pair(1)

        # header line
        stdscr.addstr(0, 0, "┌" + "─"*(w-2) + "┐", colour)
        hdr = HEADER + ("[LINK]" if connection_ok.is_set() else "[NO LINK]")
        stdscr.addstr(1, 0, "│" + hdr.ljust(w-2)[:w-2] + "│", colour)

        # message pane
        for i in range(pane_height):
            y = 2 + i
            idx = offset + i
            if idx < len(messages):
                src, txt = messages[idx]
                line = f"{src}: {txt}"
                stdscr.addstr(y, 0, line.ljust(w)[:w], colour)

        # footer
        stdscr.addstr(h-2, 0, "└" + "─"*(w-2) + "┘", colour)
        stdscr.addstr(h-1, 0, FOOTER.ljust(w)[:w], colour)

        # if in SEND mode
        if sending:
            stdscr.addstr(h-1, 0, "Send> " + input_buf[-(w-6):], colour)
            stdscr.move(h-1, 6 + len(input_buf[-(w-6):]))

        stdscr.refresh()
        curses.napms(30)

        # input ----------------------------------------------------------
        try:
            ch = stdscr.getch()
        except curses.error:
            continue

        if sending:
            if ch in (curses.KEY_ENTER, 10, 13):
                text_to_send = input_buf.strip()
                input_buf = ""
                sending = False
                if text_to_send:
                    try:
                        # quick optimistic echo to screen
                        messages.append(("You", text_to_send))
                        if connection_ok.is_set():
                            # send asynchronously to avoid UI stall
                            threading.Thread(
                                target=iface_send,
                                args=(text_to_send,),
                                daemon=True
                            ).start()
                    except Exception as e:
                        messages.append(("ERR", f"send failed: {e}"))
            elif ch in (27,):                    # Esc abort
                sending = False
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                input_buf = input_buf[:-1]
            elif 32 <= ch <= 126:               # printable ASCII
                input_buf += chr(ch)
            continue

        # normal mode ----------------------------------------------------
        if ch == curses.KEY_UP:
            offset = max(0, offset - 1)
        elif ch == curses.KEY_DOWN:
            offset = min(max_offset, offset + 1)
        elif ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
                if bstate & curses.BUTTON1_PRESSED:
                    drag_start_y = my
                elif bstate & curses.BUTTON1_RELEASED:
                    delta = my - drag_start_y
                    offset = min(max_offset, max(0, offset - delta))
            except Exception:
                pass
        elif ch in (ord('s'), ord('S')):
            sending = True
            input_buf = ""
        elif ch in (ord('q'), ord('Q'), 27):     # q or Esc
            stop_event.set()

def iface_send(text: str):
    """Send text on the wire – runs in its own thread."""
    try:
        # wait up to 5 s for a link if user pressed send too early
        if not connection_ok.wait(5):
            messages.append(("ERR", "radio not connected"))
            return
        # re‑open inside this thread: meshtastic requires per‑thread handle
        iface = mserial.SerialInterface(devPath=DEV_PATH)
        iface.sendText(text)
        iface.close()
    except Exception as e:
        messages.append(("ERR", f"radio error: {e}"))

# ── GRACEFUL SHUT‑DOWN ────────────────────────────────────────────────────────
def sig_handler(sig, _):
    stop_event.set()
signal.signal(signal.SIGINT,  sig_handler)
signal.signal(signal.SIGTERM, sig_handler)

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        curses.wrapper(run_ui)
    finally:
        stop_event.set()
        rt.join(timeout=2)
        json_fh.close()
        db.close()
