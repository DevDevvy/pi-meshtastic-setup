#!/usr/bin/env python3
"""
meshtastic‑retro‑ui.py  –  ultra‑minimal BLE version
• No connection thread, no mutexes
• Calls BLEInterface once at startup and lets the library own the link
• UI thread sends messages directly with iface.sendText()
"""

import os, json, sqlite3, signal, queue, time, curses, textwrap
import curses.textpad
from pathlib import Path
from datetime import datetime
from meshtastic.ble_interface import BLEInterface
from pubsub import pub

# ── CONFIG ───────────────────────────────────────────────────────────────────
DATA_DIR = Path.home() / ".retrobadge"; DATA_DIR.mkdir(exist_ok=True)
DB_FILE  = DATA_DIR / "meshtastic.db"
LOG_FILE = DATA_DIR / "meshtastic.log"
NODE_ADDR = os.getenv("MESHTASTIC_BLE_ADDR", "").strip()       # set in run_badge.sh
PAIR_PIN  = os.getenv("MESHTASTIC_PIN", "123456")              # if RANDOM_PIN disabled
MAX_LEN, PAD_V = 240, 2

# ── PERSISTENCE ─────────────────────────────────────────────────────────────
json_fh = open(LOG_FILE, "a", encoding="utf‑8", buffering=1)
db      = sqlite3.connect(DB_FILE, check_same_thread=False)
with db:
    db.execute("""CREATE TABLE IF NOT EXISTS messages (
                    ts REAL, src TEXT, txt TEXT)""")

# ── STATE ────────────────────────────────────────────────────────────────────
incoming_q  = queue.Queue(1024)
link_up_evt = False                 # simple bool is enough now
connection_status = "Not connected"
iface = None                        # will hold BLEInterface

# ── DB writer (still async so UI never blocks on disk I/O) ──────────────────
db_q = queue.Queue()
def db_writer():
    while True:
        ts, src, txt = db_q.get()
        with db: db.execute("INSERT INTO messages VALUES (?,?,?)", (ts, src, txt))
        db_q.task_done()
import threading; threading.Thread(target=db_writer, daemon=True).start()

# ── MESSAGE & CONNECTION HANDLERS (via pypubsub) ────────────────────────────
def on_packet(packet, *_):
    global incoming_q
    dec   = packet.get("decoded", {})
    text  = dec.get("text") or dec.get("data", {}).get("text")
    if not text:                                   # ignore non‑text packets
        return
    ts  = packet.get("rxTime", time.time()) / (1000 if packet.get("rxTime",0)>1e12 else 1)
    src = packet.get("fromId", "unknown")
    text = text[:MAX_LEN]
    incoming_q.put_nowait((ts, src, text))
    db_q.put((ts, src, text))
    json_fh.write(f"# RX {src}: {text}\n")

def on_conn_established(*_):
    global link_up_evt, connection_status
    link_up_evt, connection_status = True, "Connected"
    json_fh.write("# CONNECTION ESTABLISHED\n")

def on_conn_lost(*_):
    global link_up_evt, connection_status
    link_up_evt, connection_status = False, "Disconnected"
    json_fh.write("# CONNECTION LOST\n")

pub.subscribe(on_packet,          "meshtastic.receive")
pub.subscribe(on_conn_established,"meshtastic.connection.established")
pub.subscribe(on_conn_lost,       "meshtastic.connection.lost")

# ── UI HELPERS ───────────────────────────────────────────────────────────────
def _fmt(ts): return datetime.fromtimestamp(ts).strftime("%H:%M")
def _history(limit=2000):
    cur=db.cursor(); cur.execute("SELECT ts,src,txt FROM messages ORDER BY ts DESC LIMIT ?",(limit,))
    return list(reversed(cur.fetchall()))
def safe_footer(win,row,text,attr=0):
    h,w=win.getmaxyx(); win.addstr(row,0,text.ljust(w-1)[:w-1],attr)

# ── MAIN CURSES UI LOOP (single thread) ──────────────────────────────────────
def ui(stdscr):
    global connection_status, iface
    curses.curs_set(0); curses.noecho(); curses.cbreak()
    stdscr.keypad(True); stdscr.nodelay(True)
    curses.start_color(); curses.use_default_colors()
    for i,c in enumerate((curses.COLOR_GREEN,curses.COLOR_RED,curses.COLOR_BLUE,curses.COLOR_YELLOW),1):
        curses.init_pair(i,c,curses.COLOR_BLACK)
    text_col, no_link, yes_link, warn_col = [curses.color_pair(i) for i in range(1,5)]

    msgs=_history(); viewofs=0; send_mode=False
    TITLE=" Retro‑Meshtastic Badge — ↑/↓ scroll, S send, Q quit "

    while True:
        # pull any new messages
        while True:
            try: msgs.append(incoming_q.get_nowait())
            except queue.Empty: break
        h,w=stdscr.getmaxyx(); pane_h=h-PAD_V*2-2
        if viewofs >= len(msgs)-pane_h: viewofs=max(0,len(msgs)-pane_h) # auto‑scroll

        stdscr.erase()
        stdscr.addstr(0,0,"╔"+TITLE.center(w-2,"═")[:w-2]+"╗",text_col)

        # connection bar
        bar = "[● LINKED] " if link_up_evt else "[○ NO LINK] "
        safe_footer(stdscr,1,bar+connection_status, yes_link if link_up_evt else no_link)
        row_start=PAD_V+2

        # history
        row=used=0; idx=viewofs
        while used<pane_h and idx<len(msgs):
            ts,src,txt=msgs[idx]; prefix=f"{_fmt(ts)} {src[:10]:>10} │ "
            avail=w-len(prefix)
            for j,line in enumerate(textwrap.wrap(txt,avail) or [""]):
                if used>=pane_h:break
                out=(prefix+line if j==0 else ' '*len(prefix)+line).ljust(w)[:w]
                stdscr.addstr(row_start+used,0,out,text_col); used+=1
            idx+=1

        stdscr.addstr(h-2,0,"╚"+"═"*(w-2)+"╝",text_col)
        footer="[S]end  [Q]uit  ↑/↓ PgUp/PgDn"; safe_footer(stdscr,h-1,footer,text_col)
        stdscr.refresh(); curses.napms(100)

        # key handling
        try: c=stdscr.getch()
        except curses.error: c=-1
        if c in (3,ord('q'),ord('Q')): break
        if c in (curses.KEY_UP,):  viewofs=max(0,viewofs-1)
        if c in (curses.KEY_DOWN,):viewofs=min(len(msgs)-pane_h,viewofs+1)
        if c==curses.KEY_PPAGE:   viewofs=max(0,viewofs-pane_h)
        if c==curses.KEY_NPAGE:   viewofs=min(len(msgs)-pane_h,viewofs+pane_h)
        if c in (ord('s'),ord('S')):
            curses.curs_set(1); prompt="Send> "; stdscr.addstr(h-1,0,prompt,text_col)
            win=curses.newwin(1,w-len(prompt)-1,h-1,len(prompt)); win.keypad(True)
            tb=curses.textpad.Textbox(win,insert_mode=True)
            def end(ch): return 7 if ch in (10,13) else ch
            try: s=tb.edit(end).strip()
            finally: curses.curs_set(0)
            if s:
                ts=time.time(); msgs.append((ts,"You",s)); db_q.put((ts,"You",s))
                try: iface.sendText(s, wantAck=True)
                except Exception as e:
                    connection_status=f"Send failed: {e}"

# ── PROGRAM ENTRY ────────────────────────────────────────────────────────────
def main():
    global iface, connection_status
    if not NODE_ADDR:
        print("MESHTASTIC_BLE_ADDR not set"); return

    try:
        print(f"Connecting to {NODE_ADDR} …")
        iface = BLEInterface(address=NODE_ADDR,
                             debugOut=json_fh,
                             pairing_pin=PAIR_PIN)
        connection_status="Connected"   # until PubSub updates it
    except Exception as e:
        connection_status=f"Connect failed: {e}"
        print(connection_status)

    signal.signal(signal.SIGINT, lambda *_: exit(0))
    curses.wrapper(ui)

if __name__=="__main__":
    main()
